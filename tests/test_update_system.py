from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import helios_update
import helios_update_worker
from helios_update import UpdateManager, UpdateRelease, is_newer, sha256_file
from helios_core.signing import generate_keypair, write_signature


REQUIRED_SOURCE_FILES = (
    "helios_performance_hub.py",
    "helios_update.py",
    "helios_update_worker.py",
    "helios_launcher.py",
    "requirements.txt",
    "release.json",
)


def write_install(root: Path, version: str, marker: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in REQUIRED_SOURCE_FILES:
        if name == "release.json":
            content = json.dumps(
                {
                    "app_id": helios_update.APP_ID,
                    "version": version,
                    "updater_protocol": 1,
                    "supported_install_modes": ["source"],
                    "required_files": ["release.json"],
                    "mode_required_files": {
                        "source": [
                            "helios_performance_hub.py",
                            "helios_update.py",
                            "helios_update_worker.py",
                            "helios_launcher.py",
                            "requirements.txt",
                        ]
                    },
                }
            )
        else:
            content = f"{marker}:{name}\n"
        (root / name).write_text(content, encoding="utf-8")


class FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes):
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class VersionTests(unittest.TestCase):
    def test_semantic_version_order(self) -> None:
        self.assertTrue(is_newer("4.0.1", "4.0.0"))
        self.assertTrue(is_newer("4.1.0-beta.2", "4.1.0-beta.1"))
        self.assertTrue(is_newer("4.1.0", "4.1.0-beta.9"))
        self.assertFalse(is_newer("4.0.0", "4.0.0"))
        self.assertFalse(is_newer("bad", "4.0.0"))


class ArchiveTests(unittest.TestCase):
    def test_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            archive_path = Path(temp) / "bad.zip"
            destination = Path(temp) / "stage"
            destination.mkdir()
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../outside.txt", "bad")
            with zipfile.ZipFile(archive_path, "r") as archive:
                with self.assertRaises(helios_update.UpdateError):
                    helios_update._validated_members(archive, destination)

    def test_download_hash_and_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package_root = root / "package"
            write_install(package_root, "4.0.1", "new")
            archive_path = root / "HeliosPerformanceHub-4.0.1.zip"
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                for file in package_root.iterdir():
                    archive.write(file, file.name)
            payload = archive_path.read_bytes()
            release = UpdateRelease(
                version="4.0.1",
                tag_name="v4.0.1",
                title="Test",
                notes="Test release",
                published_at="2026-07-11T00:00:00Z",
                prerelease=False,
                release_url="https://github.com/example/helios/releases/tag/v4.0.1",
                package_name=archive_path.name,
                package_url="https://github.com/example/helios/releases/download/v4.0.1/test.zip",
                package_size=len(payload),
                sha256_name=archive_path.name + ".sha256",
                sha256_url="https://github.com/example/helios/releases/download/v4.0.1/test.zip.sha256",
                sha256=sha256_file(archive_path),
            )
            install = root / "install"
            data = root / "data"
            write_install(install, "4.0.0", "old")
            manager = UpdateManager(current_version="4.0.0", install_root=install, data_root=data)
            with mock.patch.object(helios_update, "_request", return_value=FakeResponse(payload)):
                staged = manager.download_and_stage(release)
            self.assertEqual(staged.release.version, "4.0.1")
            self.assertTrue((staged.staging_path / "helios_performance_hub.py").is_file())

    def test_trusted_install_requires_valid_signature(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package_root = root / "package"
            write_install(package_root, "5.0.0", "new")
            archive_path = root / "HeliosPerformanceHub-5.0.0.zip"
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                for file in package_root.iterdir():
                    archive.write(file, file.name)
            private_key = root / "private.pem"
            public_key = root / "public.pem"
            signature_path = root / "package.sig"
            generate_keypair(private_key, public_key)
            write_signature(archive_path, private_key, signature_path, public_key)
            payload = archive_path.read_bytes()
            release = UpdateRelease(
                version="5.0.0", tag_name="v5.0.0", title="Signed", notes="Signed release",
                published_at="2026-07-11T00:00:00Z", prerelease=False,
                release_url="https://github.com/example/helios/releases/tag/v5.0.0",
                package_name=archive_path.name,
                package_url="https://github.com/example/helios/releases/download/v5.0.0/test.zip",
                package_size=len(payload), sha256_name=archive_path.name + ".sha256",
                sha256_url="https://github.com/example/helios/releases/download/v5.0.0/test.zip.sha256",
                sha256=sha256_file(archive_path), signature_name=archive_path.name + ".sig",
                signature_url="https://github.com/example/helios/releases/download/v5.0.0/test.zip.sig",
                signature_text=signature_path.read_text(encoding="utf-8"),
            )
            install = root / "install"
            data = root / "data"
            write_install(install, "4.0.1", "old")
            (install / "release_public_key.pem").write_bytes(public_key.read_bytes())
            manager = UpdateManager(current_version="4.0.1", install_root=install, data_root=data)
            with mock.patch.object(helios_update, "_request", return_value=FakeResponse(payload)):
                staged = manager.download_and_stage(release)
            self.assertEqual(staged.release.version, "5.0.0")

            unsigned = UpdateRelease(**{**release.to_dict(), "signature_text": ""})
            with mock.patch.object(helios_update, "_request", return_value=FakeResponse(payload)):
                with self.assertRaises(helios_update.UpdateError):
                    manager.download_and_stage(unsigned)


class WorkerTests(unittest.TestCase):
    def test_install_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_root = root / "install"
            staging_root = root / "staging"
            data_root = root / "data"
            write_install(install_root, "4.0.0", "old")
            write_install(staging_root, "4.0.1", "new")
            (install_root / ".venv").mkdir()
            (install_root / ".venv" / "keep.txt").write_text("keep", encoding="utf-8")

            plan = {
                "protocol": 1,
                "action": "install",
                "current_pid": 0,
                "install_root": str(install_root),
                "staging_root": str(staging_root),
                "data_root": str(data_root),
                "status_path": str(data_root / "updates" / "status.json"),
                "history_path": str(data_root / "updates" / "history.json"),
                "pending_health_path": str(data_root / "updates" / "pending.json"),
                "healthy_path": str(data_root / "updates" / "healthy.json"),
                "from_version": "4.0.0",
                "to_version": "4.0.1",
                "relaunch_command": ["ignored"],
                "recovery_command": ["ignored"],
            }
            launches = []
            with mock.patch.object(helios_update_worker, "launch", side_effect=lambda command, cwd: launches.append((command, cwd))):
                helios_update_worker.install(plan)
            self.assertIn("new", (install_root / "helios_performance_hub.py").read_text(encoding="utf-8"))
            self.assertTrue((install_root / ".venv" / "keep.txt").is_file())
            history = json.loads(Path(plan["history_path"]).read_text(encoding="utf-8"))
            backup = Path(history["backup_path"])
            self.assertTrue(backup.is_dir())
            self.assertEqual(len(launches), 1)

            rollback_plan = {
                "protocol": 1,
                "action": "rollback",
                "current_pid": 0,
                "install_root": str(install_root),
                "backup_root": str(backup),
                "data_root": str(data_root),
                "status_path": str(data_root / "updates" / "status.json"),
                "history_path": str(data_root / "updates" / "history.json"),
                "pending_health_path": str(data_root / "updates" / "pending.json"),
                "healthy_path": str(data_root / "updates" / "healthy.json"),
                "from_version": "4.0.1",
                "to_version": "4.0.0",
                "relaunch_command": ["ignored"],
            }
            with mock.patch.object(helios_update_worker, "launch"):
                helios_update_worker.rollback(rollback_plan)
            self.assertIn("old", (install_root / "helios_performance_hub.py").read_text(encoding="utf-8"))
            self.assertTrue((install_root / ".venv" / "keep.txt").is_file())


if __name__ == "__main__":
    unittest.main()
