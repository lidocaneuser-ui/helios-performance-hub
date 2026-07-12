"""Stable launcher and update-health guard for Helios Performance Control Hub."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

APP_DATA_DIR = "HeliosPerformanceHub"
MAX_FAILED_LAUNCHES = 2
HEALTH_GRACE_SECONDS = 15 * 60


def install_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def data_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) / APP_DATA_DIR) if local else (install_root() / "data")


def read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def atomic_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temp.replace(path)




def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sync_dependencies() -> None:
    """Synchronize the preserved source-install virtual environment after updates."""
    root = install_root()
    requirements = root / "requirements.txt"
    venv_python = root / ".venv" / "Scripts" / "python.exe"
    if getattr(sys, "frozen", False) or not requirements.is_file() or not venv_python.is_file():
        return
    state_path = data_root() / "runtime" / "requirements_state.json"
    digest = sha256_file(requirements)
    state = read_json(state_path)
    if state.get("sha256") == digest:
        return
    log_path = data_root() / "logs" / "dependency_sync.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(venv_python), "-m", "pip", "install",
        "--disable-pip-version-check", "--upgrade", "-r", str(requirements),
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    completed = subprocess.run(
        command, capture_output=True, text=True, errors="replace",
        timeout=15 * 60, creationflags=flags, cwd=str(root),
    )
    log_path.write_text(
        f"Command: {' '.join(command)}\nExit: {completed.returncode}\n\nSTDOUT\n{completed.stdout}\n\nSTDERR\n{completed.stderr}\n",
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Helios could not synchronize its dependencies. Review {log_path}")
    atomic_json(
        state_path,
        {"sha256": digest, "updated_epoch": time.time(), "python": str(venv_python)},
    )

def app_command(extra: Sequence[str]) -> List[str]:
    root = install_root()
    binary = root / "HeliosPerformanceHub.exe"
    if binary.is_file():
        return [str(binary), *extra]

    script = root / "helios_performance_hub.py"
    if not script.is_file():
        raise FileNotFoundError("Helios application entry point is missing.")
    pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
    python_console = root / ".venv" / "Scripts" / "python.exe"
    runner = pythonw if pythonw.is_file() else python_console if python_console.is_file() else Path(sys.executable)
    return [str(runner), str(script), *extra]


def updater_command() -> Tuple[List[str], Path]:
    root = install_root()
    temp_root = Path(tempfile.mkdtemp(prefix="helios-launcher-rollback-"))
    binary = root / "HeliosUpdater.exe"
    if binary.is_file():
        copied = temp_root / binary.name
        shutil.copy2(binary, copied)
        return [str(copied)], temp_root

    script = root / "helios_update_worker.py"
    if not script.is_file():
        raise FileNotFoundError("Helios update worker is missing.")
    copied = temp_root / script.name
    shutil.copy2(script, copied)
    python_console = root / ".venv" / "Scripts" / "python.exe"
    runner = python_console if python_console.is_file() else Path(sys.executable)
    return [str(runner), str(copied)], temp_root


def maybe_recover_failed_update() -> bool:
    data = data_root()
    updates = data / "updates"
    pending_path = updates / "pending_health.json"
    healthy_path = updates / "healthy.json"
    history_path = updates / "last_update.json"
    status_path = updates / "worker_status.json"
    pending = read_json(pending_path)
    if not pending:
        return False

    healthy = read_json(healthy_path)
    target_version = str(pending.get("to_version", ""))
    if healthy.get("version") == target_version:
        pending_path.unlink(missing_ok=True)
        return False

    attempts = int(pending.get("launch_attempts", 0)) + 1
    pending["launch_attempts"] = attempts
    pending["last_launch_epoch"] = time.time()
    atomic_json(pending_path, pending)

    age = time.time() - float(pending.get("created_at_epoch", time.time()))
    backup = Path(str(pending.get("backup_path", "")))
    should_rollback = attempts >= MAX_FAILED_LAUNCHES or age >= HEALTH_GRACE_SECONDS
    if not should_rollback or not backup.is_dir():
        return False

    command, temp_root = updater_command()
    plan = {
        "protocol": 1,
        "action": "rollback",
        "current_pid": os.getpid(),
        "install_root": str(install_root()),
        "backup_root": str(backup),
        "data_root": str(data),
        "status_path": str(status_path),
        "history_path": str(history_path),
        "pending_health_path": str(pending_path),
        "healthy_path": str(healthy_path),
        "from_version": target_version,
        "to_version": str(pending.get("from_version", "unknown")),
        "relaunch_command": app_command(["--rollback-complete", str(pending.get("from_version", "unknown"))]),
        "worker_temp_root": str(temp_root),
    }
    plan_path = temp_root / "automatic-rollback-plan.json"
    atomic_json(plan_path, plan)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    subprocess.Popen([*command, "--plan", str(plan_path)], close_fds=True, creationflags=flags)
    return True


def main() -> int:
    try:
        if maybe_recover_failed_update():
            return 0
        sync_dependencies()
        passthrough = sys.argv[1:]
        pending = read_json(data_root() / "updates" / "pending_health.json")
        target = str(pending.get("to_version", ""))
        if target and "--post-update" not in passthrough:
            passthrough = [*passthrough, "--post-update", target]
        subprocess.Popen(app_command(passthrough), cwd=str(install_root()), close_fds=True)
        return 0
    except Exception as exc:
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Helios Launcher", str(exc))
            root.destroy()
        except Exception:
            print(f"Helios Launcher: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
