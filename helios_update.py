"""Secure update client for Helios Performance Control Hub.

The update channel is a public GitHub repository. Each release must contain:

    HeliosPerformanceHub-<version>.zip
    HeliosPerformanceHub-<version>.zip.sha256

The ZIP must contain release.json at its root with matching app_id/version values.
Updates are downloaded to LocalAppData, SHA-256 verified, validated against path
traversal/ZIP bombs, then applied by the separate helios_update_worker process.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from helios_core.signing import verify_release

APP_ID = "helios-performance-control-hub"
UPDATER_PROTOCOL_VERSION = 1
GITHUB_API_VERSION = "2026-03-10"
MAX_ARCHIVE_BYTES = 750 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 1_500 * 1024 * 1024
MAX_ARCHIVE_FILES = 8000
DOWNLOAD_CHUNK = 1024 * 1024
HTTP_TIMEOUT = 20

Logger = Callable[[str, str], None]
ProgressCallback = Callable[[int, int], None]


class UpdateError(RuntimeError):
    """Raised for a user-facing update failure."""


@dataclass(frozen=True)
class UpdateRelease:
    version: str
    tag_name: str
    title: str
    notes: str
    published_at: str
    prerelease: bool
    release_url: str
    package_name: str
    package_url: str
    package_size: int
    sha256_name: str
    sha256_url: str
    sha256: str
    signature_name: str = ""
    signature_url: str = ""
    signature_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "UpdateRelease":
        return cls(**raw)


@dataclass(frozen=True)
class StagedUpdate:
    release: UpdateRelease
    archive_path: Path
    staging_path: Path


@dataclass(frozen=True)
class RollbackInfo:
    version_before: str
    version_after: str
    backup_path: Path
    installed_at: str


_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+]([0-9A-Za-z.-]+))?$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SHA256_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")


def _prerelease_key(value: Optional[str]) -> Tuple[Any, ...]:
    if not value:
        return (1,)  # A stable release sorts after prereleases of the same base.
    parts: List[Any] = [0]
    for part in re.split(r"[.-]", value.lower()):
        if part.isdigit():
            parts.append((1, int(part)))
        else:
            parts.append((0, part))
    return tuple(parts)


def version_key(value: str) -> Tuple[Any, ...]:
    match = _VERSION_RE.match(value.strip())
    if not match:
        raise ValueError(f"Invalid semantic version: {value!r}")
    major, minor, patch = (int(match.group(i)) for i in range(1, 4))
    return major, minor, patch, _prerelease_key(match.group(4))


def is_newer(candidate: str, current: str) -> bool:
    try:
        return version_key(candidate) > version_key(current)
    except ValueError:
        return False


def normalize_version(tag: str) -> str:
    value = tag.strip()
    if value.lower().startswith("v"):
        value = value[1:]
    version_key(value)  # validate
    return value


def sha256_file(path: Path, progress: Optional[ProgressCallback] = None) -> str:
    digest = hashlib.sha256()
    total = path.stat().st_size
    completed = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(DOWNLOAD_CHUNK)
            if not block:
                break
            digest.update(block)
            completed += len(block)
            if progress:
                progress(completed, total)
    return digest.hexdigest()


def _safe_log(logger: Optional[Logger], message: str, level: str = "INFO") -> None:
    if logger:
        try:
            logger(message, level)
        except Exception:
            pass


def _request(url: str, *, accept: str, timeout: int = HTTP_TIMEOUT) -> urllib.response.addinfourl:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise UpdateError("Update downloads must use HTTPS.")
    headers = {
        "Accept": accept,
        "User-Agent": "Helios-Performance-Control-Hub-Updater",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = f"HTTP {exc.code}"
        if exc.code == 403:
            detail += " (GitHub rate limit or repository access restriction)"
        raise UpdateError(f"Update server request failed: {detail}.") from exc
    except urllib.error.URLError as exc:
        raise UpdateError(f"Could not reach the update server: {exc.reason}") from exc


def _read_json(url: str) -> Any:
    with _request(url, accept="application/vnd.github+json") as response:
        payload = response.read(4 * 1024 * 1024 + 1)
    if len(payload) > 4 * 1024 * 1024:
        raise UpdateError("The update response was unexpectedly large.")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("The update server returned invalid JSON.") from exc


def _read_small_text(url: str) -> str:
    with _request(url, accept="application/octet-stream") as response:
        payload = response.read(64 * 1024 + 1)
    if len(payload) > 64 * 1024:
        raise UpdateError("The checksum file was unexpectedly large.")
    return payload.decode("utf-8", errors="replace")


class GitHubReleaseClient:
    """Reads stable or beta releases from a public GitHub repository."""

    def __init__(self, owner: str, repository: str, logger: Optional[Logger] = None) -> None:
        owner = owner.strip()
        repository = repository.strip()
        if not _REPO_RE.fullmatch(owner) or not _REPO_RE.fullmatch(repository):
            raise UpdateError("GitHub owner and repository names are invalid.")
        self.owner = owner
        self.repository = repository
        self.logger = logger

    @property
    def releases_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repository}/releases"

    @property
    def api_url(self) -> str:
        return f"https://api.github.com/repos/{self.owner}/{self.repository}/releases?per_page=30"

    def find_update(self, current_version: str, channel: str = "stable") -> Optional[UpdateRelease]:
        channel = channel.strip().lower()
        if channel not in {"stable", "beta"}:
            raise UpdateError("The update channel must be Stable or Beta.")

        payload = _read_json(self.api_url)
        if not isinstance(payload, list):
            message = payload.get("message") if isinstance(payload, dict) else None
            raise UpdateError(message or "GitHub returned an unexpected release response.")

        candidates: List[Tuple[Tuple[Any, ...], Dict[str, Any], str]] = []
        for item in payload:
            if not isinstance(item, dict) or item.get("draft"):
                continue
            prerelease = bool(item.get("prerelease"))
            if channel == "stable" and prerelease:
                continue
            try:
                version = normalize_version(str(item.get("tag_name", "")))
                key = version_key(version)
            except ValueError:
                continue
            if is_newer(version, current_version):
                candidates.append((key, item, version))

        if not candidates:
            return None

        _, release, version = max(candidates, key=lambda row: row[0])
        assets = [asset for asset in release.get("assets", []) if isinstance(asset, dict)]
        expected_zip = f"HeliosPerformanceHub-{version}.zip"
        package = next((asset for asset in assets if asset.get("name") == expected_zip), None)
        if package is None:
            raise UpdateError(
                f"Release {version} exists, but the required asset {expected_zip} is missing."
            )

        checksum_names = {
            f"{expected_zip}.sha256",
            f"HeliosPerformanceHub-{version}.sha256",
        }
        checksum = next((asset for asset in assets if asset.get("name") in checksum_names), None)
        if checksum is None:
            raise UpdateError(f"Release {version} is missing its SHA-256 checksum asset.")

        checksum_text = _read_small_text(str(checksum.get("browser_download_url", "")))
        match = _SHA256_RE.search(checksum_text)
        if not match:
            raise UpdateError(f"Release {version} has an invalid SHA-256 checksum file.")

        package_url = str(package.get("browser_download_url", ""))
        sha_url = str(checksum.get("browser_download_url", ""))
        signature_names = {f"{expected_zip}.sig", f"HeliosPerformanceHub-{version}.sig"}
        signature_asset = next((asset for asset in assets if asset.get("name") in signature_names), None)
        signature_url = str(signature_asset.get("browser_download_url", "")) if signature_asset else ""
        signature_text = _read_small_text(signature_url) if signature_url else ""
        for url in (package_url, sha_url, signature_url):
            if not url:
                continue
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme != "https" or parsed.hostname not in {"github.com", "objects.githubusercontent.com"}:
                raise UpdateError("A release asset URL did not point to an approved GitHub HTTPS host.")

        return UpdateRelease(
            version=version,
            tag_name=str(release.get("tag_name", "")),
            title=str(release.get("name") or f"Helios {version}"),
            notes=str(release.get("body") or "No release notes were provided."),
            published_at=str(release.get("published_at") or ""),
            prerelease=bool(release.get("prerelease")),
            release_url=str(release.get("html_url") or self.releases_url),
            package_name=expected_zip,
            package_url=package_url,
            package_size=int(package.get("size") or 0),
            sha256_name=str(checksum.get("name") or ""),
            sha256_url=sha_url,
            sha256=match.group(1).lower(),
            signature_name=str(signature_asset.get("name") or "") if signature_asset else "",
            signature_url=signature_url,
            signature_text=signature_text,
        )


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _validated_members(archive: zipfile.ZipFile, destination: Path) -> List[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > MAX_ARCHIVE_FILES:
        raise UpdateError("The update archive contains too many files.")

    total = 0
    destination_resolved = destination.resolve()
    for info in members:
        total += max(0, int(info.file_size))
        if total > MAX_UNCOMPRESSED_BYTES:
            raise UpdateError("The update archive expands beyond the allowed size.")
        if _is_symlink(info):
            raise UpdateError("The update archive contains an unsupported symbolic link.")
        normalized = info.filename.replace("\\", "/")
        if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
            raise UpdateError("The update archive contains an unsafe path.")
        parts = Path(normalized).parts
        if any(part in {"..", ""} for part in parts):
            raise UpdateError("The update archive contains a path traversal entry.")
        target = (destination / Path(*parts)).resolve()
        try:
            target.relative_to(destination_resolved)
        except ValueError as exc:
            raise UpdateError("The update archive attempted to write outside staging.") from exc
    return members


def _normalize_extracted_root(staging: Path) -> Path:
    entries = [entry for entry in staging.iterdir() if entry.name not in {"__MACOSX"}]
    if len(entries) == 1 and entries[0].is_dir() and not (staging / "release.json").exists():
        return entries[0]
    return staging


class UpdateManager:
    """Coordinates checks, downloads, staging, installation, and rollback."""

    def __init__(
        self,
        *,
        current_version: str,
        install_root: Path,
        data_root: Path,
        logger: Optional[Logger] = None,
    ) -> None:
        self.current_version = current_version
        self.install_root = install_root.resolve()
        self.data_root = data_root.resolve()
        self.logger = logger
        self.updates_root = self.data_root / "updates"
        self.downloads_root = self.updates_root / "downloads"
        self.staging_root = self.updates_root / "staging"
        self.status_path = self.updates_root / "worker_status.json"
        self.history_path = self.updates_root / "last_update.json"
        self.pending_health_path = self.updates_root / "pending_health.json"
        self.healthy_path = self.updates_root / "healthy.json"
        for path in (self.updates_root, self.downloads_root, self.staging_root):
            path.mkdir(parents=True, exist_ok=True)

    def check(self, owner: str, repository: str, channel: str) -> Optional[UpdateRelease]:
        _safe_log(self.logger, f"Checking {channel} update channel at {owner}/{repository}.")
        client = GitHubReleaseClient(owner, repository, self.logger)
        result = client.find_update(self.current_version, channel)
        if result:
            _safe_log(self.logger, f"Update {result.version} is available.", "ACTION")
        else:
            _safe_log(self.logger, "No newer update is available.")
        return result

    def download_and_stage(
        self,
        release: UpdateRelease,
        progress: Optional[ProgressCallback] = None,
    ) -> StagedUpdate:
        archive_path = self.downloads_root / release.package_name
        partial_path = archive_path.with_suffix(archive_path.suffix + ".partial")
        if partial_path.exists():
            partial_path.unlink(missing_ok=True)

        _safe_log(self.logger, f"Downloading Helios {release.version}.", "ACTION")
        with _request(release.package_url, accept="application/octet-stream", timeout=45) as response:
            header_size = int(response.headers.get("Content-Length") or 0)
            expected_size = release.package_size or header_size
            if expected_size and expected_size > MAX_ARCHIVE_BYTES:
                raise UpdateError("The update package is larger than the allowed limit.")
            completed = 0
            with partial_path.open("wb") as handle:
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    completed += len(chunk)
                    if completed > MAX_ARCHIVE_BYTES:
                        raise UpdateError("The update download exceeded the allowed size.")
                    handle.write(chunk)
                    if progress:
                        progress(completed, expected_size)
                handle.flush()
                os.fsync(handle.fileno())

        if release.package_size and partial_path.stat().st_size != release.package_size:
            partial_path.unlink(missing_ok=True)
            raise UpdateError("The downloaded package size did not match the GitHub release asset.")

        actual_hash = sha256_file(partial_path)
        if actual_hash.lower() != release.sha256.lower():
            partial_path.unlink(missing_ok=True)
            raise UpdateError("Update integrity check failed: SHA-256 did not match.")
        partial_path.replace(archive_path)
        _safe_log(self.logger, f"SHA-256 verified for {release.package_name}.")

        trusted_key = self.install_root / "release_public_key.pem"
        if trusted_key.is_file() and trusted_key.stat().st_size > 0:
            if not release.signature_text:
                archive_path.unlink(missing_ok=True)
                raise UpdateError("This installation requires a signed update, but the release signature is missing.")
            verified, detail = verify_release(archive_path, release.signature_text, trusted_key)
            if not verified:
                archive_path.unlink(missing_ok=True)
                raise UpdateError(f"Update signature verification failed: {detail}")
            _safe_log(self.logger, detail, "ACTION")

        destination = self.staging_root / release.version
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        destination.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                members = _validated_members(archive, destination)
                for member in members:
                    archive.extract(member, destination)
        except zipfile.BadZipFile as exc:
            shutil.rmtree(destination, ignore_errors=True)
            raise UpdateError("The update package is not a valid ZIP archive.") from exc

        extracted_root = _normalize_extracted_root(destination)
        manifest_path = extracted_root / "release.json"
        if not manifest_path.is_file():
            shutil.rmtree(destination, ignore_errors=True)
            raise UpdateError("The update package does not contain release.json.")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            shutil.rmtree(destination, ignore_errors=True)
            raise UpdateError("The update package release manifest is invalid.") from exc

        if manifest.get("app_id") != APP_ID:
            shutil.rmtree(destination, ignore_errors=True)
            raise UpdateError("The package belongs to a different application.")
        if str(manifest.get("version")) != release.version:
            shutil.rmtree(destination, ignore_errors=True)
            raise UpdateError("The package version does not match the GitHub release.")
        if int(manifest.get("updater_protocol", 0)) > UPDATER_PROTOCOL_VERSION:
            shutil.rmtree(destination, ignore_errors=True)
            raise UpdateError("This update requires a newer updater protocol.")

        install_mode = "binary" if getattr(sys, "frozen", False) else "source"
        supported_modes = manifest.get("supported_install_modes", ["source"])
        if not isinstance(supported_modes, list) or install_mode not in supported_modes:
            raise UpdateError(f"This package does not support the current {install_mode} installation mode.")

        required_files = manifest.get("required_files", [])
        mode_required = manifest.get("mode_required_files", {})
        if not isinstance(required_files, list):
            raise UpdateError("The update package required_files field is invalid.")
        if not isinstance(mode_required, dict):
            raise UpdateError("The update package mode_required_files field is invalid.")
        selected_mode_files = mode_required.get(install_mode, [])
        if not isinstance(selected_mode_files, list):
            raise UpdateError("The update package mode file list is invalid.")
        all_required = [*required_files, *selected_mode_files]
        if not all_required:
            raise UpdateError("The update package did not declare required files.")
        missing = [name for name in all_required if not (extracted_root / str(name)).exists()]
        if missing:
            raise UpdateError("The update package is incomplete: " + ", ".join(missing[:8]))

        _safe_log(self.logger, f"Helios {release.version} was staged and validated for {install_mode} mode.", "ACTION")
        return StagedUpdate(release=release, archive_path=archive_path, staging_path=extracted_root)

    def _copy_worker_to_temp(self) -> Tuple[Sequence[str], Path]:
        temp_root = Path(tempfile.mkdtemp(prefix="helios-updater-"))
        if getattr(sys, "frozen", False):
            candidates = [
                self.install_root / "HeliosUpdater.exe",
                Path(sys.executable).resolve().with_name("HeliosUpdater.exe"),
            ]
            worker = next((path for path in candidates if path.is_file()), None)
            if worker is None:
                raise UpdateError("HeliosUpdater.exe is missing from the installation.")
            temp_worker = temp_root / "HeliosUpdater.exe"
            shutil.copy2(worker, temp_worker)
            return [str(temp_worker)], temp_root

        worker = self.install_root / "helios_update_worker.py"
        if not worker.is_file():
            worker = Path(__file__).resolve().with_name("helios_update_worker.py")
        if not worker.is_file():
            raise UpdateError("helios_update_worker.py is missing from the installation.")
        temp_worker = temp_root / "helios_update_worker.py"
        shutil.copy2(worker, temp_worker)
        return [sys.executable, str(temp_worker)], temp_root

    def _launcher_command(self, *extra: str) -> List[str]:
        if getattr(sys, "frozen", False):
            launcher = self.install_root / "HeliosLauncher.exe"
            app = self.install_root / "HeliosPerformanceHub.exe"
            executable = launcher if launcher.is_file() else app
            return [str(executable), *extra]
        launcher = self.install_root / "helios_launcher.py"
        if launcher.is_file():
            return [sys.executable, str(launcher), *extra]
        return [sys.executable, str(self.install_root / "helios_performance_hub.py"), *extra]

    def begin_install(self, staged: StagedUpdate, current_pid: int) -> None:
        worker_command, temp_root = self._copy_worker_to_temp()
        plan = {
            "protocol": UPDATER_PROTOCOL_VERSION,
            "action": "install",
            "current_pid": int(current_pid),
            "install_root": str(self.install_root),
            "staging_root": str(staged.staging_path),
            "data_root": str(self.data_root),
            "status_path": str(self.status_path),
            "history_path": str(self.history_path),
            "pending_health_path": str(self.pending_health_path),
            "healthy_path": str(self.healthy_path),
            "from_version": self.current_version,
            "to_version": staged.release.version,
            "relaunch_command": self._launcher_command("--post-update", staged.release.version),
            "recovery_command": self._launcher_command("--update-recovery", self.current_version),
            "worker_temp_root": str(temp_root),
        }
        plan_path = temp_root / "update-plan.json"
        plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        command = [*worker_command, "--plan", str(plan_path)]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        subprocess.Popen(command, close_fds=True, creationflags=flags)
        _safe_log(self.logger, f"Updater launched for Helios {staged.release.version}.", "ACTION")

    def rollback_info(self) -> Optional[RollbackInfo]:
        try:
            raw = json.loads(self.history_path.read_text(encoding="utf-8"))
            backup = Path(str(raw["backup_path"]))
            if not backup.is_dir():
                return None
            return RollbackInfo(
                version_before=str(raw.get("from_version", "unknown")),
                version_after=str(raw.get("to_version", "unknown")),
                backup_path=backup,
                installed_at=str(raw.get("installed_at", "")),
            )
        except Exception:
            return None

    def begin_rollback(self, current_pid: int) -> RollbackInfo:
        info = self.rollback_info()
        if info is None:
            raise UpdateError("No valid update backup is available to restore.")
        worker_command, temp_root = self._copy_worker_to_temp()
        plan = {
            "protocol": UPDATER_PROTOCOL_VERSION,
            "action": "rollback",
            "current_pid": int(current_pid),
            "install_root": str(self.install_root),
            "backup_root": str(info.backup_path),
            "data_root": str(self.data_root),
            "status_path": str(self.status_path),
            "history_path": str(self.history_path),
            "pending_health_path": str(self.pending_health_path),
            "healthy_path": str(self.healthy_path),
            "from_version": info.version_after,
            "to_version": info.version_before,
            "relaunch_command": self._launcher_command("--rollback-complete", info.version_before),
            "worker_temp_root": str(temp_root),
        }
        plan_path = temp_root / "rollback-plan.json"
        plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        subprocess.Popen([*worker_command, "--plan", str(plan_path)], close_fds=True, creationflags=flags)
        _safe_log(self.logger, f"Rollback launched to Helios {info.version_before}.", "ACTION")
        return info

    def mark_healthy(self) -> None:
        payload = {"version": self.current_version, "healthy_at": time.time()}
        temp = self.healthy_path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp.replace(self.healthy_path)
        try:
            pending = json.loads(self.pending_health_path.read_text(encoding="utf-8"))
            if str(pending.get("to_version")) == self.current_version:
                self.pending_health_path.unlink(missing_ok=True)
        except Exception:
            pass

    def read_worker_status(self) -> Dict[str, Any]:
        try:
            return json.loads(self.status_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def clear_stale_downloads(self, keep_version: Optional[str] = None) -> None:
        for path in self.downloads_root.iterdir():
            if keep_version and keep_version in path.name:
                continue
            try:
                if path.is_file() and time.time() - path.stat().st_mtime > 14 * 86400:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        for path in self.staging_root.iterdir():
            if keep_version and path.name == keep_version:
                continue
            try:
                if path.is_dir() and time.time() - path.stat().st_mtime > 14 * 86400:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass
