"""Out-of-process update installer for Helios Performance Control Hub.

This file is intentionally independent from the GUI application. The GUI copies
it to a temporary directory before launching it, allowing every installed file,
including the normal updater, to be replaced safely after the GUI exits.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
import shutil
import subprocess
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Iterable, Set

PROTOCOL_VERSION = 1
WAIT_TIMEOUT_SECONDS = 90
PRESERVE_NAMES: Set[str] = {".venv", "venv", ".git"}


def atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    temp.replace(path)


def write_status(plan: Dict[str, Any], state: str, message: str, **extra: Any) -> None:
    path = Path(plan["status_path"])
    atomic_json(
        path,
        {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "state": state,
            "message": message,
            "from_version": plan.get("from_version"),
            "to_version": plan.get("to_version"),
            **extra,
        },
    )


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def wait_for_exit(pid: int) -> None:
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while process_exists(pid):
        if time.monotonic() >= deadline:
            raise RuntimeError("Helios did not exit before the updater timeout.")
        time.sleep(0.25)


def ensure_safe_root(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.exists():
        raise RuntimeError(f"{label} does not exist: {resolved}")
    if resolved == resolved.anchor or len(resolved.parts) < 3:
        raise RuntimeError(f"Refusing to operate on unsafe {label}: {resolved}")
    return resolved


def copy_tree(source: Path, destination: Path, *, ignore_preserved: bool = False) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if ignore_preserved and item.name.lower() in PRESERVE_NAMES:
            continue
        target = destination / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def clear_program_root(root: Path) -> None:
    for item in root.iterdir():
        if item.name.lower() in PRESERVE_NAMES:
            continue
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        except FileNotFoundError:
            pass


def verify_install(root: Path) -> None:
    source_mode = (root / "helios_performance_hub.py").is_file()
    binary_mode = (root / "HeliosPerformanceHub.exe").is_file()
    if not source_mode and not binary_mode:
        raise RuntimeError("Updated installation has no Helios application entry point.")
    if source_mode:
        for required in ("helios_update.py", "helios_update_worker.py", "helios_launcher.py", "release.json"):
            if not (root / required).is_file():
                raise RuntimeError(f"Updated source installation is missing {required}.")
    if binary_mode and not (root / "HeliosLauncher.exe").is_file():
        raise RuntimeError("Updated binary installation is missing HeliosLauncher.exe.")


def launch(command: Iterable[str], cwd: Path) -> None:
    command_list = [str(part) for part in command]
    if not command_list:
        raise RuntimeError("The update plan did not include a relaunch command.")
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(command_list, cwd=str(cwd), close_fds=True, creationflags=flags)


def backup_directory(data_root: Path, from_version: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_version = "".join(ch for ch in from_version if ch.isalnum() or ch in ".-_") or "unknown"
    return data_root / "program_backups" / f"{safe_version}_{stamp}"


def prune_backups(data_root: Path, keep: int = 3) -> None:
    root = data_root / "program_backups"
    if not root.is_dir():
        return
    backups = sorted((path for path in root.iterdir() if path.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[keep:]:
        shutil.rmtree(old, ignore_errors=True)


def restore_backup(install_root: Path, backup_root: Path) -> None:
    clear_program_root(install_root)
    copy_tree(backup_root, install_root)
    verify_install(install_root)


def install(plan: Dict[str, Any]) -> None:
    install_root = ensure_safe_root(Path(plan["install_root"]), "installation root")
    staging_root = ensure_safe_root(Path(plan["staging_root"]), "staging root")
    data_root = Path(plan["data_root"]).resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    backup_root = backup_directory(data_root, str(plan.get("from_version", "unknown")))

    write_status(plan, "waiting", "Waiting for Helios to close.")
    wait_for_exit(int(plan.get("current_pid", 0)))
    write_status(plan, "backing_up", "Backing up the current installation.")
    backup_root.mkdir(parents=True, exist_ok=False)
    copy_tree(install_root, backup_root, ignore_preserved=True)

    try:
        write_status(plan, "installing", "Installing the staged update.", backup_path=str(backup_root))
        clear_program_root(install_root)
        copy_tree(staging_root, install_root)
        verify_install(install_root)

        installed_at = dt.datetime.now(dt.timezone.utc).isoformat()
        history = {
            "from_version": plan.get("from_version"),
            "to_version": plan.get("to_version"),
            "installed_at": installed_at,
            "backup_path": str(backup_root),
        }
        atomic_json(Path(plan["history_path"]), history)
        Path(plan["healthy_path"]).unlink(missing_ok=True)
        atomic_json(
            Path(plan["pending_health_path"]),
            {
                **history,
                "launch_attempts": 0,
                "created_at_epoch": time.time(),
            },
        )
        prune_backups(data_root)
        write_status(plan, "installed", "Update installed. Launching Helios.", backup_path=str(backup_root))
        launch(plan["relaunch_command"], install_root)
    except Exception:
        write_status(plan, "recovering", "Update failed. Restoring the previous installation.")
        try:
            restore_backup(install_root, backup_root)
            write_status(plan, "restored", "Previous installation restored after update failure.")
            launch(plan.get("recovery_command") or plan["relaunch_command"], install_root)
        except Exception as rollback_exc:
            write_status(
                plan,
                "fatal",
                "Update and automatic recovery both failed.",
                error=traceback.format_exc(),
                rollback_error=str(rollback_exc),
                backup_path=str(backup_root),
            )
        raise


def rollback(plan: Dict[str, Any]) -> None:
    install_root = ensure_safe_root(Path(plan["install_root"]), "installation root")
    backup_root = ensure_safe_root(Path(plan["backup_root"]), "backup root")
    data_root = Path(plan["data_root"]).resolve()

    write_status(plan, "waiting", "Waiting for Helios to close before rollback.")
    wait_for_exit(int(plan.get("current_pid", 0)))
    failed_backup = data_root / "failed_versions" / dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    failed_backup.mkdir(parents=True, exist_ok=False)
    copy_tree(install_root, failed_backup, ignore_preserved=True)

    write_status(plan, "rolling_back", "Restoring the previous Helios version.")
    restore_backup(install_root, backup_root)
    Path(plan["pending_health_path"]).unlink(missing_ok=True)
    Path(plan["healthy_path"]).unlink(missing_ok=True)
    Path(plan["history_path"]).unlink(missing_ok=True)
    write_status(plan, "rolled_back", "Rollback completed. Launching Helios.")
    launch(plan["relaunch_command"], install_root)


def run_plan(plan_path: Path) -> int:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if int(plan.get("protocol", 0)) != PROTOCOL_VERSION:
        raise RuntimeError("Unsupported updater protocol.")
    action = str(plan.get("action", ""))
    if action == "install":
        install(plan)
    elif action == "rollback":
        rollback(plan)
    else:
        raise RuntimeError(f"Unsupported update action: {action!r}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Helios out-of-process update worker")
    parser.add_argument("--plan", required=True, type=Path)
    args = parser.parse_args()
    try:
        return run_plan(args.plan.resolve())
    except Exception as exc:
        try:
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
            write_status(plan, "fatal", str(exc), error=traceback.format_exc())
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
