"""Build, sign, validate, and optionally publish Helios update packages."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

from helios_core.signing import (
    generate_keypair,
    keypair_matches,
    restore_public_key,
    write_signature,
)

APP_ID = "helios-performance-control-hub"
UPDATER_PROTOCOL = 1  # Kept compatible with every installed 4.x updater.
DEFAULT_KEY_ROOT = Path.home() / ".helios-release"
DEFAULT_PRIVATE_KEY = DEFAULT_KEY_ROOT / "ed25519-private.pem"
RUNTIME_FILES = [
    "helios_performance_hub.py",
    "helios_update.py",
    "helios_update_worker.py",
    "helios_launcher.py",
    "helios_release.py",
    "helios_core",
    "requirements.txt",
    "Install_and_Run.cmd",
    "Install_Helios.ps1",
    "Run_Helios.cmd",
    "Build_EXE.cmd",
    "Publish_Update.cmd",
    "Setup_Update_Repository.cmd",
    "Setup_Update_Repository.ps1",
    "Developer_Setup.cmd",
    "Migrate_Develop_And_Publish.ps1",
    "Uninstall_Helios.cmd",
    "Uninstall_Helios.ps1",
    "README.md",
    "START_HERE.txt",
    "UPDATE_SYSTEM.md",
    "PRODUCTION.md",
    "SECURITY.md",
    "RELEASE_NOTES.md",
    "CHANGELOG.md",
    "release_public_key.pem",
]
REPOSITORY_FILES = [
    *RUNTIME_FILES,
    "requirements-dev.txt",
    "pyproject.toml",
    ".gitignore",
    "LICENSE",
    "CONTRIBUTING.md",
    "tests",
    ".github",
]
BINARY_FILES = [
    "HeliosPerformanceHub.exe",
    "HeliosLauncher.exe",
    "HeliosUpdater.exe",
]


def app_version(project: Path) -> str:
    text = (project / "helios_performance_hub.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']', text, flags=re.MULTILINE)
    if not match:
        raise RuntimeError("Could not read APP_VERSION from helios_performance_hub.py")
    return match.group(1)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_required(source: Path, destination: Path, names: Iterable[str]) -> None:
    for name in names:
        item = source / name
        if not item.exists():
            raise FileNotFoundError(f"Required release file is missing: {item}")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(item, target)


def validate_package(package: Path, version: str) -> None:
    with zipfile.ZipFile(package, "r") as archive:
        names = set(archive.namelist())
        if "release.json" not in names:
            raise RuntimeError("Built package is missing release.json")
        manifest = json.loads(archive.read("release.json").decode("utf-8"))
        if manifest.get("app_id") != APP_ID or str(manifest.get("version")) != version:
            raise RuntimeError("Built package manifest does not match the release")
        for required in manifest.get("required_files", []):
            if str(required).rstrip("/") not in {name.rstrip("/") for name in names}:
                raise RuntimeError(f"Built package is missing required item: {required}")


def build_package(
    project: Path,
    output: Path,
    mode: str,
    version: str,
    *,
    private_key: Path | None = None,
) -> Sequence[Path]:
    output.mkdir(parents=True, exist_ok=True)
    package_name = f"HeliosPerformanceHub-{version}.zip"
    package = output / package_name
    checksum = output / f"{package_name}.sha256"

    with tempfile.TemporaryDirectory(prefix="helios-release-") as temp:
        staging = Path(temp) / "package"
        staging.mkdir(parents=True)
        supported: List[str] = []
        mode_required: Dict[str, List[str]] = {}

        if mode in {"source", "hybrid"}:
            copy_required(project, staging, RUNTIME_FILES)
            supported.append("source")
            mode_required["source"] = [
                "helios_performance_hub.py",
                "helios_update.py",
                "helios_update_worker.py",
                "helios_launcher.py",
                "helios_core",
                "requirements.txt",
            ]

        if mode in {"binary", "hybrid"}:
            binary_root = project / "dist"
            copy_required(binary_root, staging, BINARY_FILES)
            supported.append("binary")
            mode_required["binary"] = BINARY_FILES.copy()

        manifest = {
            "app_id": APP_ID,
            "version": version,
            "updater_protocol": UPDATER_PROTOCOL,
            "release_format": 2,
            "supported_install_modes": supported,
            "required_files": ["release.json"],
            "mode_required_files": mode_required,
        }
        (staging / "release.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        package.unlink(missing_ok=True)
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for file in sorted(path for path in staging.rglob("*") if path.is_file()):
                archive.write(file, file.relative_to(staging).as_posix())

    validate_package(package, version)
    digest = sha256(package)
    checksum.write_text(f"{digest}  {package.name}\n", encoding="utf-8")
    assets: List[Path] = [package, checksum]

    public_key = project / "release_public_key.pem"
    if private_key is not None:
        if not private_key.is_file():
            raise FileNotFoundError(f"Release signing key is missing: {private_key}")
        if not public_key.is_file() or public_key.stat().st_size == 0:
            restore_public_key(private_key, public_key)
            print(f"Recovered trusted public key from: {private_key}")
        if not keypair_matches(private_key, public_key):
            raise RuntimeError(
                "The private release key does not match release_public_key.pem. "
                "Restore the correct private key; do not publish this release."
            )
        signature = output / f"{package_name}.sig"
        write_signature(package, private_key, signature, public_key)
        assets.append(signature)
    elif public_key.is_file() and public_key.stat().st_size > 0:
        raise RuntimeError(
            "This project has a trusted public key, so unsigned releases are blocked. "
            f"Provide --signing-key or restore {DEFAULT_PRIVATE_KEY}."
        )
    return assets


def publish(repo: str, version: str, assets: Sequence[Path], notes_file: Path, prerelease: bool) -> None:
    if shutil.which("gh") is None:
        raise RuntimeError("GitHub CLI (gh) is not installed or not on PATH.")
    tag = f"v{version}"
    exists = subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0
    if exists:
        command = ["gh", "release", "upload", tag, *map(str, assets), "--clobber", "--repo", repo]
    else:
        command = [
            "gh", "release", "create", tag, *map(str, assets),
            "--repo", repo,
            "--title", f"Helios Performance Control Hub {version}",
            "--notes-file", str(notes_file),
        ]
        if prerelease:
            command.append("--prerelease")
    completed = subprocess.run(command)
    if completed.returncode != 0:
        raise RuntimeError("GitHub release publishing failed.")


def ensure_signing_key(project: Path, private_key: Path, force: bool = False) -> None:
    public_key = project / "release_public_key.pem"
    if force:
        private_key.unlink(missing_ok=True)
        public_key.unlink(missing_ok=True)

    if private_key.is_file():
        if not public_key.is_file() or public_key.stat().st_size == 0:
            restore_public_key(private_key, public_key)
            print(f"Recovered trusted public key from: {private_key}")
        elif not keypair_matches(private_key, public_key):
            raise RuntimeError(
                "The private release key does not match release_public_key.pem. "
                "Restore the correct private key. Use --force-new-key only before any signed release has shipped."
            )
        print(f"Signing key verified: {private_key}")
        print(f"Trusted public key: {public_key}")
        return

    if public_key.is_file() and public_key.stat().st_size > 0:
        raise RuntimeError(
            "release_public_key.pem is already trusted, but its private key is missing. "
            f"Restore the private key to {private_key}; generating a replacement would break future updates."
        )

    private, public = generate_keypair(private_key, public_key)
    print(f"Private release key: {private}")
    print(f"Trusted public key: {public}")
    print("Back up the private key securely. Do not commit or publish it.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a signed Helios update release")
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--mode", choices=("source", "binary", "hybrid"), default="source")
    parser.add_argument("--version", default=None)
    parser.add_argument("--notes", type=Path, default=None)
    parser.add_argument("--publish", metavar="OWNER/REPOSITORY")
    parser.add_argument("--prerelease", action="store_true")
    parser.add_argument("--signing-key", type=Path, default=None)
    parser.add_argument("--generate-signing-key", action="store_true")
    parser.add_argument("--force-new-key", action="store_true")
    parser.add_argument("--repository-bundle", action="store_true", help="Create a source repository snapshot ZIP.")
    args = parser.parse_args()

    project = args.project.resolve()
    if args.generate_signing_key:
        ensure_signing_key(project, (args.signing_key or DEFAULT_PRIVATE_KEY).expanduser().resolve(), args.force_new_key)
        if not args.publish and not args.repository_bundle:
            return 0

    version = args.version or app_version(project)
    if version != app_version(project):
        raise RuntimeError("--version must match APP_VERSION in helios_performance_hub.py")
    output = (args.output or project / "release_artifacts" / version).resolve()

    signing_key = args.signing_key.expanduser().resolve() if args.signing_key else None
    if signing_key is None and DEFAULT_PRIVATE_KEY.is_file():
        signing_key = DEFAULT_PRIVATE_KEY
    assets = build_package(project, output, args.mode, version, private_key=signing_key)

    notes_file = args.notes.resolve() if args.notes else project / "RELEASE_NOTES.md"
    if not notes_file.exists():
        notes_file = output / "release_notes.md"
        notes_file.write_text(
            f"# Helios {version}\n\n- Performance and stability improvements.\n- Verified in-app update package.\n",
            encoding="utf-8",
        )

    print(f"Built: {assets[0]}")
    print(f"Checksum: {assets[1]}")
    if len(assets) > 2:
        print(f"Signature: {assets[2]}")
    if args.repository_bundle:
        repository_zip = output / f"helios-performance-hub-source-{version}.zip"
        with tempfile.TemporaryDirectory(prefix="helios-repository-") as temporary:
            staging = Path(temporary) / "helios-performance-hub"
            staging.mkdir(parents=True)
            copy_required(project, staging, REPOSITORY_FILES)
            shutil.make_archive(str(repository_zip.with_suffix("")), "zip", staging)
        print(f"Repository bundle: {repository_zip}")
    if args.publish:
        publish(args.publish, version, assets, notes_file, args.prerelease)
        print(f"Published v{version} to {args.publish}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
