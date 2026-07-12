"""
Helios Performance Control Hub 5.0

Always-on Windows 11 monitoring and resource-management control hub.
Designed for Travis's HP OMEN MAX 16-ah0xxx, while remaining usable on other PCs.

Core safety rules:
- Never disables Windows Security.
- Never uses REALTIME process priority.
- Never overclocks or edits undocumented GPU timeout values.
- Never mass-terminates processes.
- Every startup change is backed up and reversible.
- Automatic management prefers priority/I/O tuning over killing applications.

Dependencies:
    python -m pip install psutil pystray pillow

Run:
    python helios_performance_hub.py

For a background-only launch use pythonw.exe, or build the included executable.
"""

from __future__ import annotations

import ctypes
import datetime as dt
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from helios_update import StagedUpdate, UpdateManager, UpdateRelease
from helios_core import (
    CrashGuard, HealthAssessment, HealthAnalyzer, TelemetryDatabase,
    build_html_report, run_self_diagnostics,
)

try:
    import psutil  # type: ignore
except Exception:
    psutil = None

try:
    import pystray  # type: ignore
    from PIL import Image, ImageDraw  # type: ignore
except Exception:
    pystray = None
    Image = None
    ImageDraw = None

if os.name == "nt":
    try:
        import winreg  # type: ignore
    except Exception:
        winreg = None
else:
    winreg = None


APP_NAME = "Helios Performance Control Hub"
APP_VERSION = "5.0.0"
APP_REGISTRY_NAME = "HeliosPerformanceHub"
IS_WINDOWS = os.name == "nt"
REFRESH_MS = 1100
PROCESS_REFRESH_MS = 2600

# System power plan aliases documented by Microsoft.
BALANCED_GUID = "381b4222-f694-41f0-9685-ff5bb260df2e"
HIGH_PERFORMANCE_GUID = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
ULTIMATE_SOURCE_GUID = "e9a42b02-d5df-448d-aa00-03f14749eb61"

# The app deliberately protects Windows, security, shell, audio, and driver processes.
PROTECTED_PROCESS_NAMES = {
    "system idle process", "system", "registry", "memory compression",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "svchost.exe", "fontdrvhost.exe", "dwm.exe", "explorer.exe",
    "spoolsv.exe", "audiodg.exe", "searchindexer.exe", "searchhost.exe",
    "securityhealthsystray.exe", "msmpeng.exe", "nissrv.exe", "taskhostw.exe",
    "runtimebroker.exe", "sihost.exe", "ctfmon.exe", "conhost.exe", "wudfhost.exe",
    "startmenuexperiencehost.exe", "shellexperiencehost.exe", "textinputhost.exe",
    "lockapp.exe", "applicationframehost.exe", "systemsettings.exe",
}

# These are not automatically killed. The engine can only lower their priority,
# and optionally close a very small user-approved subset during Gaming mode.
BACKGROUND_RULES: Dict[str, Dict[str, str]] = {
    "razercortex.exe": {"severity": "high", "reason": "Known optimizer overhead"},
    "razercortexservice.exe": {"severity": "high", "reason": "Razer Cortex background service"},
    "wallpaper64.exe": {"severity": "high", "reason": "Wallpaper Engine GPU/driver conflict"},
    "wallpaper32.exe": {"severity": "high", "reason": "Wallpaper Engine GPU/driver conflict"},
    "wallpaperui.exe": {"severity": "medium", "reason": "Wallpaper Engine UI"},
    "epicgameslauncher.exe": {"severity": "medium", "reason": "Idle game launcher"},
    "steamwebhelper.exe": {"severity": "low", "reason": "Steam web renderer"},
    "docker desktop.exe": {"severity": "high", "reason": "Heavy developer runtime"},
    "com.docker.backend.exe": {"severity": "high", "reason": "Docker backend"},
    "onedrive.exe": {"severity": "medium", "reason": "Background synchronization"},
    "msedge.exe": {"severity": "medium", "reason": "Browser background load"},
    "msedgewebview2.exe": {"severity": "low", "reason": "WebView background load"},
    "mscopilot.exe": {"severity": "low", "reason": "Copilot background process"},
    "copilot.exe": {"severity": "low", "reason": "Copilot background process"},
    "protonvpn.exe": {"severity": "low", "reason": "Possible latency overhead"},
    "protonvpn.launcher.exe": {"severity": "low", "reason": "VPN launcher"},
    "robloxstudioBeta.exe".lower(): {"severity": "game", "reason": "Creator workload"},
}

COMMON_GAME_EXECUTABLES = {
    "robloxplayerbeta.exe", "fortniteclient-win64-shipping.exe", "valorant-win64-shipping.exe",
    "cs2.exe", "r5apex.exe", "cod.exe", "gta5.exe", "rdr2.exe", "minecraft.windows.exe",
    "javaw.exe", "rainbowsix.exe", "thefinals.exe", "destiny2.exe", "overwatch.exe",
    "helldivers2.exe", "eldenring.exe", "starfield.exe", "cyberpunk2077.exe",
    "rocketleague.exe", "palworld-win64-shipping.exe", "marvel-win64-shipping.exe",
    "universe sandbox x64.exe",
}

STARTUP_RECOMMENDATIONS = {
    "wallpaperengine": "Disable: it crashed inside the Intel graphics driver on this PC.",
    "razercortex": "Disable: it can create CPU overhead and conflicts with this hub.",
    "docker desktop": "Disable unless Docker must start with Windows.",
    "epicgameslauncher": "Disable and open it only when needed.",
    "microsoftcopilotautolaunch": "Disable for a cleaner login.",
    "microsoftedgeautolaunch": "Disable for a cleaner login.",
    "robloxplayerbeta": "Disable tray launch; Roblox starts normally from its launcher.",
    "steam": "Optional: disable if faster login matters more than automatic updates.",
    "onedrive": "Optional: keep enabled only when constant sync is needed.",
    "proton vpn": "Optional: keep enabled only when VPN-at-login is required.",
}

THEME = {
    "window": "#dadada",
    "panel": "#eeeeee",
    "panel_alt": "#e4e4e4",
    "header": "#2c2c2c",
    "header_text": "#ffffff",
    "header_muted": "#c6c6c6",
    "text": "#171717",
    "muted": "#555555",
    "border": "#8a8a8a",
    "dark_border": "#595959",
    "good": "#1f6b35",
    "warn": "#8a5a00",
    "bad": "#9b1c1c",
    "selection": "#c8d8e8",
}


def now_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def compact_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def human_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if abs(size) < 1024.0 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def human_rate(value: float) -> str:
    return f"{human_bytes(value)}/s"


def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_command(
    args: Sequence[str],
    timeout: int = 15,
    creationflags: int = 0,
) -> Tuple[int, str, str]:
    try:
        completed = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            creationflags=creationflags,
        )
        return completed.returncode, completed.stdout.strip(), completed.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "Command timed out."
    except Exception as exc:
        return 1, "", str(exc)


def hidden_creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0


def get_app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


@dataclass
class AppPaths:
    root: Path = field(default_factory=get_app_root)
    data: Path = field(init=False)
    logs: Path = field(init=False)
    backups: Path = field(init=False)
    exports: Path = field(init=False)
    settings: Path = field(init=False)
    events: Path = field(init=False)

    def __post_init__(self) -> None:
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) / "HeliosPerformanceHub" if local else self.root / "data"
        self.data = base
        self.logs = base / "logs"
        self.backups = base / "backups"
        self.exports = base / "exports"
        self.settings = base / "settings.json"
        self.events = base / "event_history.jsonl"
        for path in (self.data, self.logs, self.backups, self.exports):
            path.mkdir(parents=True, exist_ok=True)


PATHS = AppPaths()


class HubLogger:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.path = paths.logs / f"helios_hub_{dt.date.today().isoformat()}.log"
        self._lock = threading.Lock()
        self.max_log_bytes = 8 * 1024 * 1024
        self._prune_old_logs()
        self.listeners: List[Callable[[str], None]] = []
        self.event_listeners: List[Callable[[str, Dict[str, Any]], None]] = []

    def _prune_old_logs(self, keep_days: int = 30) -> None:
        cutoff = time.time() - keep_days * 86400
        try:
            for candidate in self.paths.logs.glob("helios_hub_*.log*"):
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink(missing_ok=True)
        except Exception:
            pass

    def _rotate_if_needed(self) -> None:
        try:
            if not self.path.exists() or self.path.stat().st_size < self.max_log_bytes:
                return
            for index in range(4, 0, -1):
                source = self.path.with_suffix(self.path.suffix + f".{index}")
                destination = self.path.with_suffix(self.path.suffix + f".{index + 1}")
                if source.exists():
                    if index == 4:
                        source.unlink(missing_ok=True)
                    else:
                        source.replace(destination)
            self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))
        except Exception:
            pass

    def add_listener(self, listener: Callable[[str], None]) -> None:
        self.listeners.append(listener)

    def add_event_listener(self, listener: Callable[[str, Dict[str, Any]], None]) -> None:
        self.event_listeners.append(listener)

    def log(self, message: str, level: str = "INFO") -> None:
        line = f"[{now_text()}] [{level}] {message}"
        with self._lock:
            try:
                self._rotate_if_needed()
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except Exception:
                pass
        for listener in list(self.listeners):
            try:
                listener(line)
            except Exception:
                pass

    def event(self, event_type: str, details: Dict[str, Any]) -> None:
        payload = {"timestamp": now_text(), "type": event_type, **details}
        try:
            with self.paths.events.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, default=str) + "\n")
        except Exception:
            pass
        for listener in list(self.event_listeners):
            try:
                listener(event_type, dict(details))
            except Exception:
                pass


LOGGER = HubLogger(PATHS)


@dataclass
class Settings:
    profile: str = "Balanced"
    optimizer_enabled: bool = True
    auto_game_detection: bool = True
    lower_background_priority: bool = True
    lower_background_io: bool = True
    stop_wallpaper_during_games: bool = True
    close_approved_apps_during_games: bool = False
    restore_after_game: bool = True
    start_with_windows: bool = False
    start_minimized: bool = False
    minimize_to_tray: bool = True
    notifications: bool = True
    automatic_update_checks: bool = True
    auto_download_updates: bool = False
    update_channel: str = "stable"
    update_repository_owner: str = ""
    update_repository_name: str = "helios-performance-hub"
    update_check_interval_hours: int = 6
    last_update_check_epoch: float = 0.0
    skipped_update_version: str = ""
    cpu_pressure_threshold: int = 92
    memory_pressure_threshold: int = 88
    disk_pressure_threshold: int = 94
    pressure_seconds: int = 8
    game_exit_grace_seconds: int = 18
    game_executables: List[str] = field(default_factory=lambda: sorted(COMMON_GAME_EXECUTABLES))
    approved_close_apps: List[str] = field(default_factory=lambda: ["wallpaper64.exe", "wallpaper32.exe"])
    priority_exclusions: List[str] = field(default_factory=list)
    last_window_geometry: str = "1240x800"
    telemetry_enabled: bool = True
    telemetry_retention_days: int = 30
    telemetry_sample_interval_seconds: int = 5
    crash_safe_mode_enabled: bool = True
    history_window_minutes: int = 30

    @classmethod
    def load(cls, path: Path) -> "Settings":
        try:
            raw: Dict[str, Any] = {}
            if path.exists():
                loaded = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    raw.update(loaded)
            bootstrap = path.with_name("update_channel.json")
            if bootstrap.exists():
                configured = json.loads(bootstrap.read_text(encoding="utf-8-sig"))
                if isinstance(configured, dict):
                    raw.update(configured)
                bootstrap.unlink(missing_ok=True)
            defaults = asdict(cls())
            merged = {**defaults, **{key: value for key, value in raw.items() if key in defaults}}
            settings = cls(**merged)
            settings.game_executables = sorted({x.lower() for x in settings.game_executables if x})
            settings.approved_close_apps = sorted({x.lower() for x in settings.approved_close_apps if x})
            settings.priority_exclusions = sorted({x.lower() for x in settings.priority_exclusions if x})
            settings.update_channel = str(settings.update_channel).strip().lower()
            if settings.update_channel not in {"stable", "beta"}:
                settings.update_channel = "stable"
            settings.update_repository_owner = str(settings.update_repository_owner).strip()
            settings.update_repository_name = str(settings.update_repository_name).strip() or "helios-performance-hub"
            settings.update_check_interval_hours = max(1, min(168, int(settings.update_check_interval_hours)))
            settings.telemetry_retention_days = max(1, min(365, int(settings.telemetry_retention_days)))
            settings.telemetry_sample_interval_seconds = max(1, min(60, int(settings.telemetry_sample_interval_seconds)))
            settings.history_window_minutes = max(5, min(1440, int(settings.history_window_minutes)))
            settings.save(path)
            return settings
        except Exception as exc:
            LOGGER.log(f"Settings were invalid and defaults were loaded: {exc}", "WARN")
            settings = cls()
            settings.save(path)
            return settings

    def save(self, path: Path = PATHS.settings) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        temp.replace(path)


@dataclass
class Snapshot:
    timestamp: float
    cpu_percent: float
    cpu_frequency_mhz: float
    memory_percent: float
    memory_used: int
    memory_total: int
    swap_percent: float
    disk_percent: float
    disk_read_rate: float
    disk_write_rate: float
    network_send_rate: float
    network_recv_rate: float
    process_count: int
    thread_count: int
    handle_count: int
    battery_percent: Optional[float]
    battery_plugged: Optional[bool]
    gpu_name: str
    gpu_percent: Optional[float]
    gpu_memory_used_mb: Optional[float]
    gpu_memory_total_mb: Optional[float]
    gpu_temperature_c: Optional[float]
    gpu_power_w: Optional[float]
    foreground_pid: Optional[int]
    foreground_name: str
    active_power_plan: str


@dataclass
class ProcessRow:
    pid: int
    name: str
    cpu: float
    memory_mb: float
    threads: int
    handles: int
    status: str
    username: str
    priority: str
    executable: str


class SingleInstance:
    def __init__(self, name: str) -> None:
        self.name = name
        self.handle: Optional[int] = None

    def acquire(self) -> bool:
        if not IS_WINDOWS:
            return True
        try:
            kernel32 = ctypes.windll.kernel32
            self.handle = kernel32.CreateMutexW(None, False, self.name)
            already_exists = kernel32.GetLastError() == 183
            return not already_exists
        except Exception:
            return True

    def release(self) -> None:
        if IS_WINDOWS and self.handle:
            try:
                ctypes.windll.kernel32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None


class WindowsOps:
    @staticmethod
    def relaunch_as_admin() -> Tuple[bool, str]:
        if not IS_WINDOWS:
            return False, "Administrator relaunch is Windows-only."
        try:
            executable = sys.executable
            if getattr(sys, "frozen", False):
                params = "--admin-relaunch"
            else:
                params = f'"{Path(__file__).resolve()}" --admin-relaunch'
            result = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, str(PATHS.root), 1)
            if result > 32:
                return True, "Elevated copy launched."
            return False, f"Windows declined elevation (code {result})."
        except Exception as exc:
            return False, str(exc)

    @staticmethod
    def foreground_pid() -> Optional[int]:
        if not IS_WINDOWS:
            return None
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            pid = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return int(pid.value) or None
        except Exception:
            return None

    @staticmethod
    def power_plans() -> List[Tuple[str, str, bool]]:
        if not IS_WINDOWS:
            return []
        rc, out, err = run_command(["powercfg", "/list"], creationflags=hidden_creation_flags())
        text = out or err
        plans: List[Tuple[str, str, bool]] = []
        pattern = re.compile(
            r"Power Scheme GUID:\s*([0-9a-fA-F-]{36})\s*\((.*?)\)\s*(\*)?",
            re.IGNORECASE,
        )
        for match in pattern.finditer(text):
            plans.append((match.group(1).lower(), match.group(2).strip(), bool(match.group(3))))
        return plans

    @staticmethod
    def active_power_plan() -> Tuple[str, str]:
        for guid, name, active in WindowsOps.power_plans():
            if active:
                return guid, name
        if IS_WINDOWS:
            rc, out, _ = run_command(["powercfg", "/getactivescheme"], creationflags=hidden_creation_flags())
            if rc == 0:
                match = re.search(r"([0-9a-fA-F-]{36}).*?\((.*?)\)", out)
                if match:
                    return match.group(1).lower(), match.group(2)
        return "", "Unknown"

    @staticmethod
    def set_power_plan(guid: str) -> Tuple[bool, str]:
        rc, out, err = run_command(
            ["powercfg", "/setactive", guid],
            creationflags=hidden_creation_flags(),
        )
        if rc == 0:
            return True, f"Power plan set to {guid}."
        return False, err or out or "Power plan change failed."

    @staticmethod
    def ensure_ultimate_performance() -> Optional[str]:
        for guid, name, _ in WindowsOps.power_plans():
            if "ultimate performance" in name.lower():
                return guid
        rc, out, err = run_command(
            ["powercfg", "/duplicatescheme", ULTIMATE_SOURCE_GUID],
            creationflags=hidden_creation_flags(),
        )
        if rc == 0:
            match = re.search(r"([0-9a-fA-F-]{36})", out or err)
            if match:
                return match.group(1).lower()
        for guid, name, _ in WindowsOps.power_plans():
            if "ultimate performance" in name.lower():
                return guid
        return None

    @staticmethod
    def best_performance_plan() -> Tuple[str, str]:
        plans = WindowsOps.power_plans()
        for guid, name, _ in plans:
            if "ultimate performance" in name.lower():
                return guid, name
        for guid, name, _ in plans:
            if "high performance" in name.lower():
                return guid, name
        ultimate = WindowsOps.ensure_ultimate_performance()
        if ultimate:
            return ultimate, "Ultimate Performance"
        return BALANCED_GUID, "Balanced"

    @staticmethod
    def install_startup(enabled: bool) -> Tuple[bool, str]:
        if not IS_WINDOWS or winreg is None:
            return False, "Windows registry support is unavailable."
        try:
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    if getattr(sys, "frozen", False):
                        launcher = PATHS.root / "HeliosLauncher.exe"
                        runner = launcher if launcher.exists() else Path(sys.executable).resolve()
                        command = f'"{runner}" --minimized'
                    else:
                        pythonw = Path(sys.executable).with_name("pythonw.exe")
                        runner = pythonw if pythonw.exists() else Path(sys.executable)
                        launcher = PATHS.root / "helios_launcher.py"
                        target = launcher if launcher.exists() else Path(__file__).resolve()
                        command = f'"{runner}" "{target}" --minimized'
                    winreg.SetValueEx(key, APP_REGISTRY_NAME, 0, winreg.REG_SZ, command)
                    return True, "Helios will start when you sign in."
                try:
                    winreg.DeleteValue(key, APP_REGISTRY_NAME)
                except FileNotFoundError:
                    pass
                return True, "Start-with-Windows entry removed."
        except Exception as exc:
            return False, str(exc)

    @staticmethod
    def startup_installed() -> bool:
        if not IS_WINDOWS or winreg is None:
            return False
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_READ,
            ) as key:
                winreg.QueryValueEx(key, APP_REGISTRY_NAME)
                return True
        except Exception:
            return False

    @staticmethod
    def startup_entries() -> List[Dict[str, str]]:
        if not IS_WINDOWS or winreg is None:
            return []
        locations = [
            ("HKCU", winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
            ("HKLM", winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
            ("HKLM32", winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
        ]
        rows: List[Dict[str, str]] = []
        for hive_name, hive, path in locations:
            try:
                with winreg.OpenKey(hive, path, 0, winreg.KEY_READ) as key:
                    index = 0
                    while True:
                        try:
                            name, command, kind = winreg.EnumValue(key, index)
                        except OSError:
                            break
                        lowered = name.lower()
                        recommendation = "Keep unless you recognize it as unnecessary."
                        for token, advice in STARTUP_RECOMMENDATIONS.items():
                            if token in lowered or token in str(command).lower():
                                recommendation = advice
                                break
                        rows.append(
                            {
                                "name": name,
                                "command": str(command),
                                "hive": hive_name,
                                "path": path,
                                "kind": str(kind),
                                "recommendation": recommendation,
                            }
                        )
                        index += 1
            except (PermissionError, FileNotFoundError, OSError):
                continue
        return sorted(rows, key=lambda row: row["name"].lower())

    @staticmethod
    def disable_startup_entry(row: Dict[str, str]) -> Tuple[bool, str]:
        if not IS_WINDOWS or winreg is None:
            return False, "Registry support is unavailable."
        hive_map = {
            "HKCU": winreg.HKEY_CURRENT_USER,
            "HKLM": winreg.HKEY_LOCAL_MACHINE,
            "HKLM32": winreg.HKEY_LOCAL_MACHINE,
        }
        hive = hive_map.get(row["hive"])
        if hive is None:
            return False, "Unknown registry hive."
        backup_path = PATHS.backups / "startup_entries.json"
        try:
            backups: List[Dict[str, str]] = []
            if backup_path.exists():
                backups = json.loads(backup_path.read_text(encoding="utf-8"))
            identity = f'{row["hive"]}|{row["path"]}|{row["name"]}'
            if not any(item.get("identity") == identity for item in backups):
                backups.append({**row, "identity": identity, "disabled_at": now_text()})
                backup_path.write_text(json.dumps(backups, indent=2), encoding="utf-8")
            with winreg.OpenKey(hive, row["path"], 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, row["name"])
            return True, f'Disabled startup entry "{row["name"]}".'
        except FileNotFoundError:
            return False, "The entry no longer exists."
        except PermissionError:
            return False, "Administrator permission is required for this entry."
        except Exception as exc:
            return False, str(exc)

    @staticmethod
    def restore_startup_entries() -> Tuple[int, List[str]]:
        if not IS_WINDOWS or winreg is None:
            return 0, ["Registry support is unavailable."]
        backup_path = PATHS.backups / "startup_entries.json"
        if not backup_path.exists():
            return 0, ["No startup backup exists."]
        hive_map = {
            "HKCU": winreg.HKEY_CURRENT_USER,
            "HKLM": winreg.HKEY_LOCAL_MACHINE,
            "HKLM32": winreg.HKEY_LOCAL_MACHINE,
        }
        restored = 0
        errors: List[str] = []
        try:
            backups = json.loads(backup_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return 0, [f"Backup could not be read: {exc}"]
        remaining = []
        for row in backups:
            hive = hive_map.get(row.get("hive"))
            if hive is None:
                errors.append(f'Unknown hive for {row.get("name", "entry")}')
                remaining.append(row)
                continue
            try:
                kind = int(row.get("kind", str(winreg.REG_SZ)))
                with winreg.CreateKeyEx(hive, row["path"], 0, winreg.KEY_SET_VALUE) as key:
                    winreg.SetValueEx(key, row["name"], 0, kind, row["command"])
                restored += 1
            except Exception as exc:
                errors.append(f'{row.get("name", "entry")}: {exc}')
                remaining.append(row)
        if remaining:
            backup_path.write_text(json.dumps(remaining, indent=2), encoding="utf-8")
        else:
            try:
                backup_path.unlink()
            except Exception:
                pass
        return restored, errors

    @staticmethod
    def pagefile_status() -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "automatic": None,
            "allocated_mb": None,
            "current_usage_mb": None,
            "peak_usage_mb": None,
            "description": "Unavailable",
        }
        if not IS_WINDOWS:
            return result
        script = (
            "$cs=Get-CimInstance Win32_ComputerSystem;"
            "$pf=Get-CimInstance Win32_PageFileUsage | Select-Object -First 1;"
            "$cfg=@(Get-CimInstance Win32_PageFileSetting | Select-Object Name,InitialSize,MaximumSize);"
            "[pscustomobject]@{Automatic=[bool]$cs.AutomaticManagedPagefile;"
            "Allocated=$pf.AllocatedBaseSize;Current=$pf.CurrentUsage;Peak=$pf.PeakUsage;Settings=$cfg}"
            "|ConvertTo-Json -Depth 5 -Compress"
        )
        rc, out, err = run_command(
            ["powershell.exe", "-NoProfile", "-Command", script],
            timeout=15,
            creationflags=hidden_creation_flags(),
        )
        if rc != 0 or not out:
            result["description"] = err or "Page-file query failed."
            return result
        try:
            raw = json.loads(out)
            result.update(
                {
                    "automatic": bool(raw.get("Automatic")),
                    "allocated_mb": raw.get("Allocated"),
                    "current_usage_mb": raw.get("Current"),
                    "peak_usage_mb": raw.get("Peak"),
                    "settings": raw.get("Settings") or [],
                }
            )
            if result["automatic"]:
                result["description"] = f'Windows-managed ({result["allocated_mb"] or "dynamic"} MB currently allocated)'
            else:
                result["description"] = f'Fixed/manual ({result["allocated_mb"] or "unknown"} MB)'
        except Exception as exc:
            result["description"] = f"Page-file response could not be parsed: {exc}"
        return result

    @staticmethod
    def enable_automatic_pagefile() -> Tuple[bool, str]:
        if not is_admin():
            return False, "Administrator permission is required."
        backup_path = PATHS.backups / "pagefile.json"
        current = WindowsOps.pagefile_status()
        try:
            if not backup_path.exists():
                backup_path.write_text(json.dumps({"created_at": now_text(), **current}, indent=2), encoding="utf-8")
            script = (
                "$cs=Get-CimInstance Win32_ComputerSystem;"
                "Set-CimInstance -InputObject $cs -Property @{AutomaticManagedPagefile=$true} | Out-Null;"
                "$verified=(Get-CimInstance Win32_ComputerSystem).AutomaticManagedPagefile;"
                "if(-not $verified){exit 7}"
            )
            rc, out, err = run_command(
                ["powershell.exe", "-NoProfile", "-Command", script],
                timeout=30,
                creationflags=hidden_creation_flags(),
            )
            if rc == 0:
                return True, "Windows-managed virtual memory is enabled. Restart Windows to fully apply it."
            return False, err or out or f"PowerShell exited with code {rc}."
        except Exception as exc:
            return False, str(exc)

    @staticmethod
    def restore_pagefile_backup() -> Tuple[bool, str]:
        if not is_admin():
            return False, "Administrator permission is required."
        backup_path = PATHS.backups / "pagefile.json"
        if not backup_path.exists():
            return False, "No page-file backup exists."
        try:
            backup = json.loads(backup_path.read_text(encoding="utf-8"))
            automatic = bool(backup.get("automatic"))
            settings = backup.get("settings") or []
            if isinstance(settings, dict):
                settings = [settings]
            encoded = json.dumps(settings, separators=(",", ":")).replace("'", "''")
            script = (
                f"$automatic=${str(automatic).lower()};"
                f"$saved=ConvertFrom-Json '{encoded}';"
                "$cs=Get-CimInstance Win32_ComputerSystem;"
                "Set-CimInstance -InputObject $cs -Property @{AutomaticManagedPagefile=$automatic} | Out-Null;"
                "if(-not $automatic){"
                "Get-CimInstance Win32_PageFileSetting -ErrorAction SilentlyContinue | Remove-CimInstance -ErrorAction SilentlyContinue;"
                "foreach($item in @($saved)){"
                "$props=@{Name=[string]$item.Name;InitialSize=[uint32]$item.InitialSize;MaximumSize=[uint32]$item.MaximumSize};"
                "New-CimInstance -ClassName Win32_PageFileSetting -Property $props -ErrorAction Stop | Out-Null"
                "}}"
            )
            rc, out, err = run_command(
                ["powershell.exe", "-NoProfile", "-Command", script],
                timeout=35,
                creationflags=hidden_creation_flags(),
            )
            if rc == 0:
                return True, "The saved page-file configuration was restored. Restart Windows to fully apply it."
            return False, err or out or f"PowerShell exited with code {rc}."
        except Exception as exc:
            return False, str(exc)

    @staticmethod
    def clear_graphics_caches() -> Tuple[int, List[str]]:
        targets: List[Path] = []
        local = os.environ.get("LOCALAPPDATA")
        if local:
            targets.extend(
                [
                    Path(local) / "D3DSCache",
                    Path(local) / "NVIDIA" / "DXCache",
                    Path(local) / "NVIDIA" / "GLCache",
                    Path(local) / "Intel" / "ShaderCache",
                ]
            )
        removed = 0
        errors: List[str] = []
        for path in targets:
            if not path.exists():
                continue
            try:
                for child in path.iterdir():
                    try:
                        if child.is_dir():
                            shutil.rmtree(child)
                        else:
                            child.unlink()
                        removed += 1
                    except Exception as exc:
                        if len(errors) < 20:
                            errors.append(f"{child}: {exc}")
            except Exception as exc:
                errors.append(f"{path}: {exc}")
        return removed, errors

    @staticmethod
    def safe_temp_cleanup() -> Tuple[int, int, List[str]]:
        targets: List[Path] = []
        try:
            targets.append(Path(tempfile.gettempdir()))
        except Exception:
            pass
        local = os.environ.get("LOCALAPPDATA")
        if local:
            targets.append(Path(local) / "Temp")
        seen: set[str] = set()
        files = 0
        dirs = 0
        errors: List[str] = []
        for path in targets:
            try:
                path = path.resolve()
            except Exception:
                continue
            key = str(path).lower()
            if key in seen or not path.exists():
                continue
            seen.add(key)
            try:
                children = list(path.iterdir())
            except Exception as exc:
                errors.append(f"{path}: {exc}")
                continue
            for child in children:
                try:
                    if child.is_dir():
                        shutil.rmtree(child)
                        dirs += 1
                    else:
                        child.unlink()
                        files += 1
                except Exception as exc:
                    if len(errors) < 30:
                        errors.append(f"{child}: {exc}")
        return files, dirs, errors

    @staticmethod
    def nvidia_stats() -> Dict[str, Any]:
        fields = [
            "name", "utilization.gpu", "memory.used", "memory.total",
            "temperature.gpu", "power.draw", "driver_version",
        ]
        rc, out, _ = run_command(
            [
                "nvidia-smi",
                f"--query-gpu={','.join(fields)}",
                "--format=csv,noheader,nounits",
            ],
            timeout=5,
            creationflags=hidden_creation_flags(),
        )
        if rc != 0 or not out:
            return {}
        line = out.splitlines()[0]
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != len(fields):
            return {}
        def num(value: str) -> Optional[float]:
            try:
                return float(value)
            except Exception:
                return None
        return {
            "name": parts[0],
            "utilization": num(parts[1]),
            "memory_used": num(parts[2]),
            "memory_total": num(parts[3]),
            "temperature": num(parts[4]),
            "power": num(parts[5]),
            "driver": parts[6],
        }

    @staticmethod
    def event_log_summary(hours: int = 72) -> List[Dict[str, str]]:
        if not IS_WINDOWS:
            return []
        start = (dt.datetime.now() - dt.timedelta(hours=hours)).isoformat()
        script = (
            f"$start=[datetime]'{start}';"
            "$logs=@('System','Application');"
            "$events=foreach($log in $logs){Get-WinEvent -FilterHashtable @{LogName=$log;StartTime=$start;Level=1,2,3} "
            "-ErrorAction SilentlyContinue | Select-Object -First 80 TimeCreated,Id,LevelDisplayName,ProviderName,Message};"
            "$events | Sort-Object TimeCreated -Descending | Select-Object -First 100 | ConvertTo-Json -Depth 4 -Compress"
        )
        rc, out, _ = run_command(
            ["powershell.exe", "-NoProfile", "-Command", script],
            timeout=25,
            creationflags=hidden_creation_flags(),
        )
        if rc != 0 or not out:
            return []
        try:
            raw = json.loads(out)
            if isinstance(raw, dict):
                raw = [raw]
            rows = []
            for item in raw:
                message = str(item.get("Message") or "").replace("\r", " ").replace("\n", " ")
                rows.append(
                    {
                        "time": str(item.get("TimeCreated") or ""),
                        "id": str(item.get("Id") or ""),
                        "level": str(item.get("LevelDisplayName") or ""),
                        "provider": str(item.get("ProviderName") or ""),
                        "message": re.sub(r"\s+", " ", message)[:300],
                    }
                )
            return rows
        except Exception:
            return []


class ResourceSampler:
    def __init__(self) -> None:
        self.last_disk = None
        self.last_net = None
        self.last_time = time.monotonic()
        self._process_aggregate = (0, 0, 0)
        self._process_aggregate_at = 0.0
        self._gpu_cache: Dict[str, Any] = {}
        self._gpu_cache_at = 0.0
        self._power_plan_name = "Unknown"
        self._power_plan_at = 0.0
        if psutil is not None:
            try:
                self.last_disk = psutil.disk_io_counters()
                self.last_net = psutil.net_io_counters()
                psutil.cpu_percent(interval=None)
            except Exception:
                pass

    def sample(self) -> Snapshot:
        if psutil is None:
            raise RuntimeError("psutil is required. Run: python -m pip install psutil")
        now = time.monotonic()
        elapsed = max(0.2, now - self.last_time)
        self.last_time = now

        cpu = float(psutil.cpu_percent(interval=None))
        freq = psutil.cpu_freq()
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk_now = psutil.disk_io_counters()
        net_now = psutil.net_io_counters()
        previous_disk = self.last_disk
        previous_net = self.last_net

        disk_read = disk_write = net_send = net_recv = 0.0
        if previous_disk and disk_now:
            disk_read = max(0.0, (disk_now.read_bytes - previous_disk.read_bytes) / elapsed)
            disk_write = max(0.0, (disk_now.write_bytes - previous_disk.write_bytes) / elapsed)
        if previous_net and net_now:
            net_send = max(0.0, (net_now.bytes_sent - previous_net.bytes_sent) / elapsed)
            net_recv = max(0.0, (net_now.bytes_recv - previous_net.bytes_recv) / elapsed)
        self.last_disk = disk_now
        self.last_net = net_now

        disk_percent = 0.0
        if previous_disk and disk_now:
            previous_busy = getattr(previous_disk, "busy_time", None)
            current_busy = getattr(disk_now, "busy_time", None)
            if previous_busy is not None and current_busy is not None:
                busy_delta_ms = max(0.0, float(current_busy - previous_busy))
                disk_percent = clamp((busy_delta_ms / (elapsed * 1000.0)) * 100.0, 0.0, 100.0)
            else:
                combined = disk_read + disk_write
                disk_percent = clamp((combined / (450 * 1024 * 1024)) * 100.0, 0.0, 100.0)

        if now - self._process_aggregate_at >= 3.0:
            processes = 0
            threads = 0
            handles = 0
            for proc in psutil.process_iter(["num_threads"]):
                processes += 1
                try:
                    threads += int(proc.info.get("num_threads") or 0)
                    if IS_WINDOWS:
                        handles += int(proc.num_handles())
                except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                    continue
            self._process_aggregate = (processes, threads, handles)
            self._process_aggregate_at = now
        processes, threads, handles = self._process_aggregate

        battery = None
        try:
            battery = psutil.sensors_battery()
        except Exception:
            pass

        if now - self._gpu_cache_at >= 2.0:
            self._gpu_cache = WindowsOps.nvidia_stats()
            self._gpu_cache_at = now
        gpu = self._gpu_cache
        foreground_pid = WindowsOps.foreground_pid()
        foreground_name = ""
        if foreground_pid:
            try:
                foreground_name = psutil.Process(foreground_pid).name()
            except Exception:
                pass
        if now - self._power_plan_at >= 5.0:
            _, self._power_plan_name = WindowsOps.active_power_plan()
            self._power_plan_at = now
        power_name = self._power_plan_name

        return Snapshot(
            timestamp=time.time(),
            cpu_percent=cpu,
            cpu_frequency_mhz=float(freq.current) if freq else 0.0,
            memory_percent=float(memory.percent),
            memory_used=int(memory.used),
            memory_total=int(memory.total),
            swap_percent=float(swap.percent),
            disk_percent=disk_percent,
            disk_read_rate=disk_read,
            disk_write_rate=disk_write,
            network_send_rate=net_send,
            network_recv_rate=net_recv,
            process_count=processes,
            thread_count=threads,
            handle_count=handles,
            battery_percent=float(battery.percent) if battery else None,
            battery_plugged=bool(battery.power_plugged) if battery else None,
            gpu_name=str(gpu.get("name") or "NVIDIA GPU not available"),
            gpu_percent=gpu.get("utilization"),
            gpu_memory_used_mb=gpu.get("memory_used"),
            gpu_memory_total_mb=gpu.get("memory_total"),
            gpu_temperature_c=gpu.get("temperature"),
            gpu_power_w=gpu.get("power"),
            foreground_pid=foreground_pid,
            foreground_name=foreground_name,
            active_power_plan=power_name,
        )

    @staticmethod
    def process_rows() -> List[ProcessRow]:
        if psutil is None:
            return []
        rows: List[ProcessRow] = []
        for proc in psutil.process_iter(
            ["pid", "name", "cpu_percent", "memory_info", "num_threads", "status", "username", "exe"]
        ):
            try:
                info = proc.info
                handles = proc.num_handles() if IS_WINDOWS else 0
                try:
                    priority_value = proc.nice()
                    priority = priority_name(priority_value)
                except Exception:
                    priority = "Unknown"
                rows.append(
                    ProcessRow(
                        pid=int(info["pid"]),
                        name=str(info.get("name") or "Unknown"),
                        cpu=float(info.get("cpu_percent") or 0.0),
                        memory_mb=float(getattr(info.get("memory_info"), "rss", 0)) / (1024 * 1024),
                        threads=int(info.get("num_threads") or 0),
                        handles=int(handles or 0),
                        status=str(info.get("status") or ""),
                        username=str(info.get("username") or ""),
                        priority=priority,
                        executable=str(info.get("exe") or ""),
                    )
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue
        rows.sort(key=lambda row: (row.cpu, row.memory_mb), reverse=True)
        return rows


def priority_name(value: Any) -> str:
    if psutil is None:
        return str(value)
    mapping = {}
    for name in (
        "IDLE_PRIORITY_CLASS", "BELOW_NORMAL_PRIORITY_CLASS", "NORMAL_PRIORITY_CLASS",
        "ABOVE_NORMAL_PRIORITY_CLASS", "HIGH_PRIORITY_CLASS", "REALTIME_PRIORITY_CLASS",
    ):
        if hasattr(psutil, name):
            mapping[getattr(psutil, name)] = name.replace("_PRIORITY_CLASS", "").title().replace("_", " ")
    return mapping.get(value, str(value))


def set_process_priority(pid: int, target: str) -> Tuple[bool, str]:
    if psutil is None:
        return False, "psutil is unavailable."
    values = {
        "Idle": getattr(psutil, "IDLE_PRIORITY_CLASS", None),
        "Below Normal": getattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS", None),
        "Normal": getattr(psutil, "NORMAL_PRIORITY_CLASS", None),
        "Above Normal": getattr(psutil, "ABOVE_NORMAL_PRIORITY_CLASS", None),
        "High": getattr(psutil, "HIGH_PRIORITY_CLASS", None),
    }
    value = values.get(target)
    if value is None:
        return False, "That priority is unsupported."
    try:
        proc = psutil.Process(pid)
        name = proc.name()
        if name.lower() in PROTECTED_PROCESS_NAMES and target not in {"Normal"}:
            return False, f"{name} is protected from automatic priority changes."
        proc.nice(value)
        return True, f"{name} ({pid}) priority set to {target}."
    except psutil.NoSuchProcess:
        return False, "The process already exited."
    except psutil.AccessDenied:
        return False, "Access denied. Try the administrator button."
    except Exception as exc:
        return False, str(exc)


def set_process_io_priority(pid: int, low: bool) -> Tuple[bool, str]:
    if psutil is None or not IS_WINDOWS:
        return False, "Windows I/O priority control is unavailable."
    try:
        proc = psutil.Process(pid)
        target = getattr(psutil, "IOPRIO_VERYLOW", 0) if low else getattr(psutil, "IOPRIO_NORMAL", 2)
        proc.ionice(target)
        return True, f"{proc.name()} I/O priority set to {'Very Low' if low else 'Normal'}."
    except psutil.AccessDenied:
        return False, "Access denied while changing I/O priority."
    except Exception as exc:
        return False, str(exc)


class AutoOptimizer:
    def __init__(self, settings: Settings, logger: HubLogger) -> None:
        self.settings = settings
        self.logger = logger
        self.lock = threading.RLock()
        self.active_game_pid: Optional[int] = None
        self.active_game_name = ""
        self.game_started_at: Optional[float] = None
        self.last_game_seen: Optional[float] = None
        self.saved_power_plan: Tuple[str, str] = ("", "")
        self.saved_priorities: Dict[int, Any] = {}
        self.saved_io: Dict[int, Any] = {}
        self.pressure_saved_priorities: Dict[int, Any] = {}
        self.pressure_saved_io: Dict[int, Any] = {}
        self.closed_apps: List[Tuple[str, str]] = []
        self.pressure_started: Dict[str, Optional[float]] = {"cpu": None, "memory": None, "disk": None}
        self.last_pressure_action: Dict[str, float] = {"cpu": 0, "memory": 0, "disk": 0}
        self.paused = False
        self.status_text = "Monitoring"

    def update_settings(self, settings: Settings) -> None:
        with self.lock:
            self.settings = settings

    def is_game_name(self, name: str) -> bool:
        lowered = name.lower()
        return lowered in {x.lower() for x in self.settings.game_executables}

    def process_snapshot(self, snapshot: Snapshot) -> None:
        with self.lock:
            if self.paused or not self.settings.optimizer_enabled:
                self.status_text = "Paused"
                return
            self.status_text = "Monitoring"
            self._handle_game_detection(snapshot)
            self._handle_pressure("cpu", snapshot.cpu_percent, self.settings.cpu_pressure_threshold)
            self._handle_pressure("memory", snapshot.memory_percent, self.settings.memory_pressure_threshold)
            self._handle_pressure("disk", snapshot.disk_percent, self.settings.disk_pressure_threshold)
            if (
                self.active_game_pid is None
                and all(value is None for value in self.pressure_started.values())
                and (self.pressure_saved_priorities or self.pressure_saved_io)
                and time.monotonic() - max(self.last_pressure_action.values()) >= 15
            ):
                self._restore_pressure_changes("resource pressure cleared")

    def _handle_game_detection(self, snapshot: Snapshot) -> None:
        if not self.settings.auto_game_detection:
            return
        pid = snapshot.foreground_pid
        name = snapshot.foreground_name
        detected = bool(pid and name and self.is_game_name(name))
        if detected:
            self.last_game_seen = time.monotonic()
            if self.active_game_pid != pid:
                if self.active_game_pid is not None:
                    self._restore_game_session("A different game became active")
                self._begin_game_session(pid, name)
            else:
                self.status_text = f"Gaming boost: {name}"
            return

        if self.active_game_pid is not None:
            game_still_running = False
            if psutil is not None:
                try:
                    game_still_running = psutil.pid_exists(self.active_game_pid)
                except Exception:
                    pass
            since_seen = time.monotonic() - (self.last_game_seen or time.monotonic())
            if not game_still_running or since_seen >= self.settings.game_exit_grace_seconds:
                self._restore_game_session("Game closed or lost focus")

    def _begin_game_session(self, pid: int, name: str) -> None:
        if psutil is None:
            return
        self.active_game_pid = pid
        self.active_game_name = name
        self.game_started_at = time.monotonic()
        self.last_game_seen = time.monotonic()
        self.saved_power_plan = WindowsOps.active_power_plan()
        self.saved_priorities.clear()
        self.saved_io.clear()
        self.closed_apps.clear()

        target_plan, target_name = WindowsOps.best_performance_plan()
        ok, message = WindowsOps.set_power_plan(target_plan)
        self.logger.log(message, "ACTION" if ok else "WARN")

        try:
            proc = psutil.Process(pid)
            self.saved_priorities[pid] = proc.nice()
            high = getattr(psutil, "HIGH_PRIORITY_CLASS", None)
            if high is not None:
                proc.nice(high)
                self.logger.log(f"Gaming boost raised {name} ({pid}) to High priority.", "ACTION")
        except Exception as exc:
            self.logger.log(f"Could not raise game priority: {exc}", "WARN")

        if self.settings.lower_background_priority or self.settings.lower_background_io:
            self._deprioritize_background(exclude_pid=pid, reason="game start", group="game")
        if self.settings.stop_wallpaper_during_games:
            self._close_named_apps({"wallpaper64.exe", "wallpaper32.exe", "wallpaperui.exe"}, approved_only=False)
        if self.settings.close_approved_apps_during_games:
            self._close_named_apps(set(self.settings.approved_close_apps), approved_only=True)

        self.status_text = f"Gaming boost: {name}"
        self.logger.event(
            "game_boost_started",
            {"pid": pid, "name": name, "power_plan": target_name},
        )

    def _deprioritize_background(self, exclude_pid: Optional[int], reason: str, group: str = "pressure") -> int:
        if psutil is None:
            return 0
        changed = 0
        exclusions = set(self.settings.priority_exclusions)
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                pid = int(proc.info["pid"])
                name = str(proc.info.get("name") or "").lower()
                if pid == exclude_pid or name in PROTECTED_PROCESS_NAMES or name in exclusions:
                    continue
                rule = BACKGROUND_RULES.get(name)
                if not rule or rule["severity"] == "game":
                    continue
                priority_store = self.saved_priorities if group == "game" else self.pressure_saved_priorities
                io_store = self.saved_io if group == "game" else self.pressure_saved_io
                if self.settings.lower_background_priority:
                    if pid not in priority_store:
                        priority_store[pid] = proc.nice()
                    below = getattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS", None)
                    if below is not None:
                        proc.nice(below)
                if self.settings.lower_background_io:
                    try:
                        if pid not in io_store:
                            io_store[pid] = proc.ionice()
                        proc.ionice(getattr(psutil, "IOPRIO_VERYLOW", 0))
                    except Exception:
                        pass
                changed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception:
                continue
        if changed:
            self.logger.log(f"Deprioritized {changed} background process(es) because of {reason}.", "ACTION")
        return changed

    def _close_named_apps(self, names: set[str], approved_only: bool) -> int:
        if psutil is None:
            return 0
        closed = 0
        approved = set(self.settings.approved_close_apps)
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                name = str(proc.info.get("name") or "").lower()
                if name not in names or name in PROTECTED_PROCESS_NAMES:
                    continue
                if approved_only and name not in approved:
                    continue
                executable = str(proc.info.get("exe") or "")
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except psutil.TimeoutExpired:
                    proc.kill()
                self.closed_apps.append((name, executable))
                closed += 1
                self.logger.log(f"Closed {name} for the gaming session.", "ACTION")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception as exc:
                self.logger.log(f"Could not close a background app: {exc}", "WARN")
        return closed

    def _restore_game_session(self, reason: str) -> None:
        if psutil is None:
            self.active_game_pid = None
            return
        if self.settings.restore_after_game:
            if self.saved_power_plan[0]:
                ok, message = WindowsOps.set_power_plan(self.saved_power_plan[0])
                self.logger.log(f"Restore power plan: {message}", "ACTION" if ok else "WARN")
            for pid, value in list(self.saved_priorities.items()):
                try:
                    psutil.Process(pid).nice(value)
                except Exception:
                    pass
            for pid, value in list(self.saved_io.items()):
                try:
                    psutil.Process(pid).ionice(value)
                except Exception:
                    pass
            for name, executable in self.closed_apps:
                if executable and Path(executable).exists():
                    try:
                        subprocess.Popen([executable], creationflags=hidden_creation_flags())
                    except Exception:
                        pass
        self.logger.log(f"Gaming boost ended: {reason}.", "ACTION")
        self.logger.event(
            "game_boost_ended",
            {"pid": self.active_game_pid, "name": self.active_game_name, "reason": reason},
        )
        self.active_game_pid = None
        self.active_game_name = ""
        self.game_started_at = None
        self.last_game_seen = None
        self.saved_power_plan = ("", "")
        self.saved_priorities.clear()
        self.saved_io.clear()
        self.closed_apps.clear()
        self.status_text = "Monitoring"

    def _handle_pressure(self, kind: str, value: float, threshold: int) -> None:
        now = time.monotonic()
        if value >= threshold:
            if self.pressure_started[kind] is None:
                self.pressure_started[kind] = now
            sustained = now - (self.pressure_started[kind] or now)
            cooldown = now - self.last_pressure_action[kind]
            if sustained >= self.settings.pressure_seconds and cooldown >= 45:
                changed = self._deprioritize_background(
                    exclude_pid=self.active_game_pid,
                    reason=f"sustained {kind} pressure ({value:.0f}%)",
                    group="game" if self.active_game_pid is not None else "pressure",
                )
                self.last_pressure_action[kind] = now
                self.logger.event(
                    "resource_pressure",
                    {"resource": kind, "value": value, "threshold": threshold, "processes_changed": changed},
                )
                self.pressure_started[kind] = now
        else:
            self.pressure_started[kind] = None

    def _restore_pressure_changes(self, reason: str) -> None:
        if psutil is None:
            self.pressure_saved_priorities.clear()
            self.pressure_saved_io.clear()
            return
        restored = 0
        for pid, value in list(self.pressure_saved_priorities.items()):
            try:
                psutil.Process(pid).nice(value)
                restored += 1
            except Exception:
                pass
        for pid, value in list(self.pressure_saved_io.items()):
            try:
                psutil.Process(pid).ionice(value)
            except Exception:
                pass
        self.pressure_saved_priorities.clear()
        self.pressure_saved_io.clear()
        if restored:
            self.logger.log(f"Restored {restored} temporary background priority change(s): {reason}.", "ACTION")

    def apply_profile(self, profile: str) -> Tuple[bool, str]:
        self.settings.profile = profile
        if profile == "Balanced":
            guid, name = BALANCED_GUID, "Balanced"
        elif profile in {"Gaming", "Maximum Performance", "Creator"}:
            guid, name = WindowsOps.best_performance_plan()
        elif profile == "Quiet":
            guid, name = BALANCED_GUID, "Balanced"
        else:
            return False, "Unknown profile."
        ok, message = WindowsOps.set_power_plan(guid)
        if ok:
            self.logger.log(f"Applied {profile} profile using {name} power plan.", "ACTION")
            self.settings.save()
            self.status_text = f"Profile: {profile}"
            return True, f"{profile} profile applied. Active plan: {name}."
        return False, message

    def shutdown(self) -> None:
        with self.lock:
            if self.active_game_pid is not None:
                self._restore_game_session("Helios is closing")
            if self.pressure_saved_priorities or self.pressure_saved_io:
                self._restore_pressure_changes("Helios is closing")


class MonitorThread(threading.Thread):
    def __init__(self, output: queue.Queue, optimizer: AutoOptimizer, stop_event: threading.Event) -> None:
        super().__init__(name="HeliosMonitor", daemon=True)
        self.output = output
        self.optimizer = optimizer
        self.stop_event = stop_event
        self.sampler = ResourceSampler()
        self.process_counter = 0

    def run(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                snapshot = self.sampler.sample()
                self.optimizer.process_snapshot(snapshot)
                self.output.put(("snapshot", snapshot))
                self.process_counter += 1
                if self.process_counter >= max(1, int(PROCESS_REFRESH_MS / REFRESH_MS)):
                    self.process_counter = 0
                    self.output.put(("processes", ResourceSampler.process_rows()))
            except Exception as exc:
                self.output.put(("monitor_error", str(exc)))
                LOGGER.log(f"Monitor error: {exc}", "ERROR")
            duration = time.monotonic() - started
            self.stop_event.wait(max(0.15, REFRESH_MS / 1000.0 - duration))


class MetricPanel(tk.Frame):
    def __init__(self, parent: tk.Widget, title: str) -> None:
        super().__init__(parent, bg=THEME["panel"], bd=1, relief="sunken", highlightthickness=0)
        self.title_label = tk.Label(
            self, text=title.upper(), bg=THEME["panel"], fg=THEME["muted"],
            font=("Segoe UI Semibold", 9), anchor="w",
        )
        self.title_label.pack(fill="x", padx=10, pady=(8, 0))
        self.value_label = tk.Label(
            self, text="--", bg=THEME["panel"], fg=THEME["text"],
            font=("Segoe UI Semibold", 20), anchor="w",
        )
        self.value_label.pack(fill="x", padx=10, pady=(0, 1))
        self.detail_label = tk.Label(
            self, text="", bg=THEME["panel"], fg=THEME["muted"],
            font=("Segoe UI", 8), anchor="w",
        )
        self.detail_label.pack(fill="x", padx=10)
        self.canvas = tk.Canvas(self, height=9, bg="#bdbdbd", highlightthickness=1, highlightbackground=THEME["border"])
        self.canvas.pack(fill="x", padx=10, pady=(6, 9))
        self.bar = self.canvas.create_rectangle(0, 0, 0, 20, fill=THEME["good"], outline="")
        self.percent = 0.0
        self.bind("<Configure>", self._redraw)

    def _redraw(self, _event: Any = None) -> None:
        width = max(1, self.canvas.winfo_width())
        fill = THEME["good"]
        if self.percent >= 90:
            fill = THEME["bad"]
        elif self.percent >= 75:
            fill = THEME["warn"]
        self.canvas.coords(self.bar, 0, 0, width * clamp(self.percent / 100.0, 0, 1), 20)
        self.canvas.itemconfigure(self.bar, fill=fill)

    def set(self, value: str, percent: float, detail: str = "") -> None:
        self.value_label.configure(text=value)
        self.detail_label.configure(text=detail)
        self.percent = float(percent)
        self._redraw()


class HeliosHub(tk.Tk):
    def __init__(self, minimized: bool = False) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1240x800")
        self.minsize(1040, 700)
        self.configure(bg=THEME["window"])
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.settings = Settings.load(PATHS.settings)
        self.crash_guard = CrashGuard(PATHS.data, APP_VERSION)
        self.safe_mode = self.crash_guard.begin() if self.settings.crash_safe_mode_enabled else False
        self.health_analyzer = HealthAnalyzer()
        self.current_assessment = HealthAssessment(100, "READY", "Waiting for telemetry.", tuple())
        self.telemetry = TelemetryDatabase(
            PATHS.data / "telemetry" / "helios_telemetry.db",
            version=APP_VERSION,
            retention_days=self.settings.telemetry_retention_days,
            sample_interval_seconds=self.settings.telemetry_sample_interval_seconds,
        )
        self.telemetry.start_session(safe_mode=self.safe_mode)
        LOGGER.add_event_listener(lambda event_type, details: self.telemetry.record_action(event_type, details))
        if self.settings.last_window_geometry:
            try:
                self.geometry(self.settings.last_window_geometry)
            except Exception:
                pass
        self.output_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.optimizer = AutoOptimizer(self.settings, LOGGER)
        if self.safe_mode:
            self.optimizer.paused = True
            self.optimizer.status_text = "Safe mode"
        self.monitor = MonitorThread(self.output_queue, self.optimizer, self.stop_event)
        self.latest_snapshot: Optional[Snapshot] = None
        self.latest_processes: List[ProcessRow] = []
        self.process_sort_column = "CPU %"
        self.process_sort_reverse = True
        self.tray_icon = None
        self.event_rows: List[Dict[str, str]] = []
        self._log_lines: List[str] = []
        self._shutting_down = False
        self.update_manager = UpdateManager(
            current_version=APP_VERSION,
            install_root=PATHS.root,
            data_root=PATHS.data,
            logger=LOGGER.log,
        )
        self.available_update: Optional[UpdateRelease] = None
        self.staged_update: Optional[StagedUpdate] = None
        self.update_busy = False
        self.update_download_was_automatic = False
        self._health_refresh_after_id: Optional[str] = None

        self._build_styles()
        self._build_ui()
        LOGGER.add_listener(self._queue_log_line)
        self.monitor.start()
        self.after(150, self._poll_queue)
        self.after(500, self._initial_refresh)
        self.after(1000, self._start_tray)
        self.after(3500, self._startup_update_check)
        self.after(4500, self._refresh_health_tab)
        self.after(6000, self._mark_update_healthy)

        if minimized or self.settings.start_minimized:
            self.after(350, self.hide_to_tray)

        LOGGER.log(f"{APP_NAME} {APP_VERSION} started. Admin={is_admin()} SafeMode={self.safe_mode}")
        if self.safe_mode:
            LOGGER.log("Crash-loop protection started Helios in safe mode; automatic optimization is paused.", "WARN")

    def _build_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background=THEME["window"])
        style.configure("Panel.TFrame", background=THEME["panel"])
        style.configure("TLabel", background=THEME["window"], foreground=THEME["text"], font=("Segoe UI", 9))
        style.configure("TButton", font=("Segoe UI", 9), padding=(9, 5))
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 9), padding=(10, 6))
        style.configure("TCheckbutton", background=THEME["window"], foreground=THEME["text"])
        style.configure("TNotebook", background=THEME["window"], borderwidth=0)
        style.configure("TNotebook.Tab", font=("Segoe UI Semibold", 9), padding=(16, 8), background="#c8c8c8")
        style.map("TNotebook.Tab", background=[("selected", THEME["panel"]), ("active", "#dedede")])
        style.configure(
            "Treeview",
            background="#f8f8f8",
            fieldbackground="#f8f8f8",
            foreground=THEME["text"],
            rowheight=25,
            font=("Segoe UI", 9),
        )
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9), background="#d0d0d0", relief="raised")
        style.map("Treeview", background=[("selected", THEME["selection"])], foreground=[("selected", THEME["text"])])
        style.configure("Horizontal.TProgressbar", troughcolor="#bcbcbc", background=THEME["good"], bordercolor=THEME["border"])

    def _build_ui(self) -> None:
        header = tk.Frame(self, bg=THEME["header"], height=78)
        header.pack(fill="x")
        header.pack_propagate(False)
        title_box = tk.Frame(header, bg=THEME["header"])
        title_box.pack(side="left", fill="both", expand=True, padx=22, pady=10)
        tk.Label(
            title_box,
            text="HELIOS  /  PERFORMANCE CONTROL HUB",
            bg=THEME["header"], fg=THEME["header_text"],
            font=("Segoe UI Semibold", 18), anchor="w",
        ).pack(fill="x")
        self.header_subtitle = tk.Label(
            title_box,
            text=f"BUILD {APP_VERSION}  /  ALWAYS-ON RESOURCE MANAGEMENT",
            bg=THEME["header"], fg=THEME["header_muted"],
            font=("Segoe UI", 9), anchor="w",
        )
        self.header_subtitle.pack(fill="x")

        header_actions = tk.Frame(header, bg=THEME["header"])
        header_actions.pack(side="right", padx=18)
        self.admin_label = tk.Label(
            header_actions,
            text="ADMIN" if is_admin() else "STANDARD",
            bg=THEME["header"], fg=THEME["header_text"],
            font=("Segoe UI Semibold", 9), padx=8,
        )
        self.admin_label.pack(side="left", padx=4)
        ttk.Button(header_actions, text="RUN AS ADMIN", command=self._relaunch_admin).pack(side="left", padx=4)
        ttk.Button(header_actions, text="HIDE TO TRAY", command=self.hide_to_tray).pack(side="left", padx=4)

        status = tk.Frame(self, bg=THEME["panel_alt"], bd=1, relief="sunken", height=36)
        status.pack(fill="x", padx=12, pady=(10, 0))
        status.pack_propagate(False)
        self.engine_status = tk.Label(
            status, text="ENGINE: STARTING", bg=THEME["panel_alt"], fg=THEME["good"],
            font=("Segoe UI Semibold", 9), anchor="w",
        )
        self.engine_status.pack(side="left", padx=10, fill="y")
        self.foreground_status = tk.Label(
            status, text="FOREGROUND: --", bg=THEME["panel_alt"], fg=THEME["muted"],
            font=("Segoe UI", 9), anchor="w",
        )
        self.foreground_status.pack(side="left", padx=18, fill="y")
        self.power_status = tk.Label(
            status, text="POWER: --", bg=THEME["panel_alt"], fg=THEME["muted"],
            font=("Segoe UI", 9), anchor="w",
        )
        self.power_status.pack(side="left", padx=18, fill="y")
        self.clock_status = tk.Label(
            status, text="", bg=THEME["panel_alt"], fg=THEME["muted"],
            font=("Segoe UI", 9), anchor="e",
        )
        self.clock_status.pack(side="right", padx=10, fill="y")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=10)
        self._build_overview_tab()
        self._build_health_tab()
        self._build_process_tab()
        self._build_optimizer_tab()
        self._build_startup_tab()
        self._build_stability_tab()
        self._build_updates_tab()
        self._build_logs_tab()

    def _new_tab(self, title: str) -> ttk.Frame:
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text=title)
        return frame

    def _build_overview_tab(self) -> None:
        tab = self._new_tab("OVERVIEW")
        tab.columnconfigure((0, 1, 2, 3), weight=1, uniform="metrics")
        tab.rowconfigure(2, weight=1)

        self.metric_cpu = MetricPanel(tab, "CPU")
        self.metric_ram = MetricPanel(tab, "Memory")
        self.metric_gpu = MetricPanel(tab, "NVIDIA GPU")
        self.metric_disk = MetricPanel(tab, "Disk Activity")
        for index, panel in enumerate((self.metric_cpu, self.metric_ram, self.metric_gpu, self.metric_disk)):
            panel.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 5, 0 if index == 3 else 5), pady=(0, 10))

        quick = tk.LabelFrame(
            tab, text=" QUICK PROFILES ", bg=THEME["window"], fg=THEME["text"],
            font=("Segoe UI Semibold", 9), bd=1, relief="groove",
        )
        quick.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(0, 10))
        for profile in ("Balanced", "Gaming", "Creator", "Maximum Performance", "Quiet"):
            ttk.Button(quick, text=profile.upper(), command=lambda p=profile: self._apply_profile(p)).pack(side="left", padx=6, pady=8)
        ttk.Separator(quick, orient="vertical").pack(side="left", fill="y", padx=8, pady=6)
        ttk.Button(quick, text="SAFE CLEANUP", command=self._safe_cleanup).pack(side="left", padx=6, pady=8)
        ttk.Button(quick, text="REFRESH NOW", command=self._manual_refresh).pack(side="left", padx=6, pady=8)
        ttk.Button(quick, text="EXPORT SNAPSHOT", command=self._export_snapshot).pack(side="left", padx=6, pady=8)

        left = tk.LabelFrame(
            tab, text=" LIVE DETAILS ", bg=THEME["window"], fg=THEME["text"],
            font=("Segoe UI Semibold", 9), bd=1, relief="groove",
        )
        left.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=(0, 5))
        self.live_details = tk.Text(
            left, bg="#f8f8f8", fg=THEME["text"], font=("Consolas", 9),
            relief="sunken", bd=1, wrap="word", state="disabled",
        )
        self.live_details.pack(fill="both", expand=True, padx=8, pady=8)

        right = tk.LabelFrame(
            tab, text=" HELIOS ANALYSIS ", bg=THEME["window"], fg=THEME["text"],
            font=("Segoe UI Semibold", 9), bd=1, relief="groove",
        )
        right.grid(row=2, column=2, columnspan=2, sticky="nsew", padx=(5, 0))
        self.analysis_text = tk.Text(
            right, bg="#f8f8f8", fg=THEME["text"], font=("Segoe UI", 9),
            relief="sunken", bd=1, wrap="word", state="disabled",
        )
        self.analysis_text.pack(fill="both", expand=True, padx=8, pady=8)

    def _build_health_tab(self) -> None:
        tab = self._new_tab("HEALTH & HISTORY")
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(1, weight=1)
        tab.rowconfigure(2, weight=1)

        summary = tk.LabelFrame(
            tab, text=" PRODUCTION HEALTH ", bg=THEME["window"], fg=THEME["text"],
            font=("Segoe UI Semibold", 9), bd=1, relief="groove",
        )
        summary.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        self.health_score_label = tk.Label(
            summary, text="100 / 100", bg=THEME["window"], fg=THEME["good"],
            font=("Segoe UI Semibold", 22), width=12, anchor="w",
        )
        self.health_score_label.pack(side="left", padx=14, pady=10)
        self.health_summary_label = tk.Label(
            summary, text="Waiting for telemetry.", bg=THEME["window"], fg=THEME["text"],
            font=("Segoe UI", 10), anchor="w", justify="left",
        )
        self.health_summary_label.pack(side="left", fill="x", expand=True, padx=8)
        self.safe_mode_label = tk.Label(
            summary,
            text="SAFE MODE" if self.safe_mode else "NORMAL MODE",
            bg=THEME["window"], fg=THEME["warn"] if self.safe_mode else THEME["good"],
            font=("Segoe UI Semibold", 10),
        )
        self.safe_mode_label.pack(side="right", padx=14)

        graph_frame = tk.LabelFrame(
            tab, text=" LAST 30 MINUTES ", bg=THEME["window"], fg=THEME["text"],
            font=("Segoe UI Semibold", 9), bd=1, relief="groove",
        )
        graph_frame.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(0, 10))
        graph_controls = tk.Frame(graph_frame, bg=THEME["window"])
        graph_controls.pack(fill="x", padx=8, pady=(6, 0))
        self.history_window_var = tk.IntVar(value=self.settings.history_window_minutes)
        tk.Label(graph_controls, text="Window:", bg=THEME["window"], fg=THEME["text"]).pack(side="left")
        ttk.Combobox(
            graph_controls, textvariable=self.history_window_var, width=7, state="readonly",
            values=(5, 15, 30, 60, 180, 720, 1440),
        ).pack(side="left", padx=6)
        ttk.Button(graph_controls, text="REFRESH", command=self._refresh_health_tab).pack(side="left", padx=4)
        ttk.Button(graph_controls, text="EXPORT HTML REPORT", command=self._export_health_report).pack(side="right", padx=4)
        ttk.Button(graph_controls, text="RUN SELF-DIAGNOSTICS", command=self._run_diagnostics_dialog).pack(side="right", padx=4)
        self.history_canvas = tk.Canvas(
            graph_frame, bg="#f8f8f8", highlightthickness=1, highlightbackground=THEME["border"], height=260,
        )
        self.history_canvas.pack(fill="both", expand=True, padx=8, pady=8)
        self.history_canvas.bind("<Configure>", lambda _event: self._draw_history_graph())
        self.history_samples: List[Dict[str, Any]] = []

        actions = tk.LabelFrame(
            tab, text=" OPTIMIZER AUDIT TRAIL ", bg=THEME["window"], fg=THEME["text"],
            font=("Segoe UI Semibold", 9), bd=1, relief="groove",
        )
        actions.grid(row=2, column=0, sticky="nsew", padx=(0, 5))
        columns = ("Time", "Event", "Details")
        self.action_tree = ttk.Treeview(actions, columns=columns, show="headings", height=8)
        self.action_tree.heading("Time", text="Time")
        self.action_tree.heading("Event", text="Event")
        self.action_tree.heading("Details", text="Details")
        self.action_tree.column("Time", width=90, anchor="center")
        self.action_tree.column("Event", width=160, anchor="w")
        self.action_tree.column("Details", width=390, anchor="w")
        self.action_tree.pack(fill="both", expand=True, padx=8, pady=8)

        controls = tk.LabelFrame(
            tab, text=" RUNTIME CONTROL ", bg=THEME["window"], fg=THEME["text"],
            font=("Segoe UI Semibold", 9), bd=1, relief="groove",
        )
        controls.grid(row=2, column=1, sticky="nsew", padx=(5, 0))
        self.session_summary_text = tk.Text(
            controls, bg="#f8f8f8", fg=THEME["text"], font=("Consolas", 9),
            relief="sunken", bd=1, wrap="word", height=9, state="disabled",
        )
        self.session_summary_text.pack(fill="both", expand=True, padx=8, pady=8)
        buttons = tk.Frame(controls, bg=THEME["window"])
        buttons.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(buttons, text="OPEN TELEMETRY FOLDER", command=lambda: self._open_folder(PATHS.data / "telemetry")).pack(side="left", padx=3)
        ttk.Button(buttons, text="RESET SAFE MODE", command=self._reset_safe_mode).pack(side="left", padx=3)
        ttk.Button(buttons, text="CLEAR OLD HISTORY", command=self._clear_telemetry_history).pack(side="right", padx=3)

    def _refresh_health_tab(self) -> None:
        if self._shutting_down:
            return
        if self._health_refresh_after_id is not None:
            try:
                self.after_cancel(self._health_refresh_after_id)
            except Exception:
                pass
            self._health_refresh_after_id = None
        try:
            minutes = int(self.history_window_var.get())
            self.settings.history_window_minutes = minutes
            self.settings.save()
            self.history_samples = self.telemetry.recent_samples(minutes=minutes, limit=1200)
            self._draw_history_graph()
            actions = self.telemetry.recent_actions(limit=80)
            self.action_tree.delete(*self.action_tree.get_children())
            for row in actions:
                details = json.dumps(row.get("details", {}), default=str, sort_keys=True)
                self.action_tree.insert(
                    "", "end",
                    values=(
                        dt.datetime.fromtimestamp(float(row.get("timestamp", 0))).strftime("%H:%M:%S"),
                        row.get("event_type", ""),
                        details[:260],
                    ),
                )
            summary = self.telemetry.session_summary()
            uptime = max(0, int(time.time() - self.telemetry.started_epoch))
            lines = [
                f"Session ID: {self.telemetry.session_id[:12]}",
                f"Uptime: {uptime // 3600:02d}:{(uptime % 3600) // 60:02d}:{uptime % 60:02d}",
                f"Samples: {summary.get('samples', 0) or 0}",
                f"Average CPU: {float(summary.get('avg_cpu') or 0):.1f}%",
                f"Peak CPU: {float(summary.get('peak_cpu') or 0):.1f}%",
                f"Average memory: {float(summary.get('avg_memory') or 0):.1f}%",
                f"Peak memory: {float(summary.get('peak_memory') or 0):.1f}%",
                f"Minimum health score: {int(summary.get('min_health') or self.current_assessment.score)}",
                f"Telemetry retention: {self.settings.telemetry_retention_days} days",
                f"Database: {self.telemetry.path}",
            ]
            self._set_text(self.session_summary_text, "\n".join(lines))
        except Exception as exc:
            LOGGER.log(f"Health history refresh failed: {exc}", "WARN")
        if not self._shutting_down:
            self._health_refresh_after_id = self.after(5000, self._refresh_health_tab)

    def _draw_history_graph(self) -> None:
        if not hasattr(self, "history_canvas"):
            return
        canvas = self.history_canvas
        canvas.delete("all")
        width = max(300, canvas.winfo_width())
        height = max(180, canvas.winfo_height())
        margin_left, margin_right, margin_top, margin_bottom = 42, 18, 20, 28
        plot_w = width - margin_left - margin_right
        plot_h = height - margin_top - margin_bottom
        for percent in (0, 25, 50, 75, 100):
            y = margin_top + plot_h - (percent / 100.0) * plot_h
            canvas.create_line(margin_left, y, width - margin_right, y, fill="#dddddd")
            canvas.create_text(margin_left - 8, y, text=str(percent), anchor="e", fill=THEME["muted"], font=("Segoe UI", 8))
        samples = self.history_samples
        if len(samples) < 2:
            canvas.create_text(width / 2, height / 2, text="Collecting performance history...", fill=THEME["muted"], font=("Segoe UI", 10))
            return
        series = [
            ("CPU", "cpu_percent", "#202020"),
            ("RAM", "memory_percent", "#6b6b6b"),
            ("DISK", "disk_percent", "#8a5a00"),
            ("GPU", "gpu_percent", "#1f6b35"),
        ]
        for label, key, color in series:
            points: List[float] = []
            for index, row in enumerate(samples):
                value = float(row.get(key) or 0.0)
                x = margin_left + (index / max(1, len(samples) - 1)) * plot_w
                y = margin_top + plot_h - (clamp(value, 0, 100) / 100.0) * plot_h
                points.extend((x, y))
            if len(points) >= 4:
                canvas.create_line(*points, fill=color, width=2, smooth=False)
        x = margin_left
        for label, _key, color in series:
            canvas.create_rectangle(x, height - 18, x + 10, height - 8, fill=color, outline=color)
            canvas.create_text(x + 15, height - 13, text=label, anchor="w", fill=THEME["text"], font=("Segoe UI", 8))
            x += 82

    def _run_diagnostics_dialog(self) -> None:
        diagnostics = run_self_diagnostics(
            app_root=PATHS.root,
            data_root=PATHS.data,
            settings_path=PATHS.settings,
            database=self.telemetry,
        )
        text = "\n".join(f"[{row['status']}] {row['name']}: {row['detail']}" for row in diagnostics)
        LOGGER.event("self_diagnostics", {"results": diagnostics})
        messagebox.showinfo("Helios self-diagnostics", text)

    def _export_health_report(self) -> None:
        diagnostics = run_self_diagnostics(
            app_root=PATHS.root,
            data_root=PATHS.data,
            settings_path=PATHS.settings,
            database=self.telemetry,
        )
        destination = PATHS.exports / f"Helios_Performance_Report_{compact_stamp()}.html"
        build_html_report(
            destination,
            app_version=APP_VERSION,
            machine_name=platform.node(),
            samples=self.telemetry.recent_samples(minutes=int(self.history_window_var.get()), limit=1600),
            actions=self.telemetry.recent_actions(limit=100),
            summary=self.telemetry.session_summary(),
            diagnostics=diagnostics,
        )
        LOGGER.event("performance_report_exported", {"path": str(destination)})
        webbrowser.open(destination.as_uri())

    def _reset_safe_mode(self) -> None:
        self.crash_guard.reset()
        self.safe_mode = False
        self.safe_mode_label.configure(text="NORMAL MODE", fg=THEME["good"])
        self.optimizer.paused = False
        LOGGER.event("safe_mode_reset", {"manual": True})
        messagebox.showinfo("Safe mode", "Crash-loop state was reset and the optimizer was resumed.")

    def _clear_telemetry_history(self) -> None:
        if not messagebox.askyesno("Clear history", "Delete completed-session telemetry while keeping the current session?"):
            return
        try:
            self.telemetry.clear_history()
            LOGGER.event("telemetry_history_cleared", {"current_session_preserved": True})
            self._refresh_health_tab()
        except Exception as exc:
            messagebox.showerror("Clear history", str(exc))

    def _build_process_tab(self) -> None:
        tab = self._new_tab("PROCESSES")
        controls = tk.Frame(tab, bg=THEME["window"])
        controls.pack(fill="x", pady=(0, 8))
        tk.Label(controls, text="Filter:", bg=THEME["window"], fg=THEME["text"]).pack(side="left")
        self.process_filter = tk.StringVar()
        entry = ttk.Entry(controls, textvariable=self.process_filter, width=26)
        entry.pack(side="left", padx=6)
        entry.bind("<KeyRelease>", lambda _e: self._populate_process_tree())
        ttk.Button(controls, text="REFRESH", command=self._refresh_processes).pack(side="left", padx=4)
        ttk.Button(controls, text="HIGH PRIORITY", command=lambda: self._selected_priority("High")).pack(side="left", padx=4)
        ttk.Button(controls, text="NORMAL PRIORITY", command=lambda: self._selected_priority("Normal")).pack(side="left", padx=4)
        ttk.Button(controls, text="LOWER BACKGROUND", command=self._manual_deprioritize).pack(side="left", padx=4)
        ttk.Button(controls, text="END SELECTED", command=self._end_selected_process).pack(side="right", padx=4)

        columns = ("PID", "Name", "CPU %", "Memory MB", "Priority", "Threads", "Handles", "Status", "User")
        self.process_tree = ttk.Treeview(tab, columns=columns, show="headings", selectmode="browse")
        widths = {"PID": 75, "Name": 210, "CPU %": 80, "Memory MB": 100, "Priority": 105, "Threads": 75, "Handles": 75, "Status": 85, "User": 170}
        for column in columns:
            self.process_tree.heading(column, text=column, command=lambda c=column: self._sort_processes(c))
            self.process_tree.column(column, width=widths[column], anchor="w" if column in {"Name", "Priority", "Status", "User"} else "center")
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=self.process_tree.yview)
        self.process_tree.configure(yscrollcommand=scrollbar.set)
        self.process_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.process_tree.bind("<Double-1>", self._show_process_details)

    def _build_optimizer_tab(self) -> None:
        tab = self._new_tab("AUTO OPTIMIZER")
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(1, weight=1)

        top = tk.LabelFrame(
            tab, text=" ENGINE CONTROL ", bg=THEME["window"], fg=THEME["text"],
            font=("Segoe UI Semibold", 9), bd=1, relief="groove",
        )
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        self.var_optimizer = tk.BooleanVar(value=self.settings.optimizer_enabled)
        self.var_game_detect = tk.BooleanVar(value=self.settings.auto_game_detection)
        self.var_lower_cpu = tk.BooleanVar(value=self.settings.lower_background_priority)
        self.var_lower_io = tk.BooleanVar(value=self.settings.lower_background_io)
        self.var_stop_wallpaper = tk.BooleanVar(value=self.settings.stop_wallpaper_during_games)
        self.var_close_approved = tk.BooleanVar(value=self.settings.close_approved_apps_during_games)
        self.var_restore = tk.BooleanVar(value=self.settings.restore_after_game)
        toggles = [
            ("Enable automatic optimizer", self.var_optimizer),
            ("Detect games from foreground app", self.var_game_detect),
            ("Lower background CPU priority", self.var_lower_cpu),
            ("Lower background disk priority", self.var_lower_io),
            ("Close Wallpaper Engine while gaming", self.var_stop_wallpaper),
            ("Close approved apps while gaming", self.var_close_approved),
            ("Restore all changes after game closes", self.var_restore),
        ]
        for index, (text, var) in enumerate(toggles):
            ttk.Checkbutton(top, text=text, variable=var, command=self._save_optimizer_settings).grid(
                row=index // 4, column=index % 4, sticky="w", padx=12, pady=7,
            )

        rules = tk.LabelFrame(
            tab, text=" PRESSURE RULES ", bg=THEME["window"], fg=THEME["text"],
            font=("Segoe UI Semibold", 9), bd=1, relief="groove",
        )
        rules.grid(row=1, column=0, sticky="nsew", padx=(0, 5))
        self.cpu_threshold = tk.IntVar(value=self.settings.cpu_pressure_threshold)
        self.ram_threshold = tk.IntVar(value=self.settings.memory_pressure_threshold)
        self.disk_threshold = tk.IntVar(value=self.settings.disk_pressure_threshold)
        self.pressure_seconds = tk.IntVar(value=self.settings.pressure_seconds)
        controls = [
            ("CPU pressure threshold", self.cpu_threshold, 70, 100, "%"),
            ("Memory pressure threshold", self.ram_threshold, 70, 100, "%"),
            ("Disk pressure threshold", self.disk_threshold, 70, 100, "%"),
            ("Sustained duration", self.pressure_seconds, 3, 30, "sec"),
        ]
        for row, (text, var, minimum, maximum, suffix) in enumerate(controls):
            tk.Label(rules, text=text, bg=THEME["window"], fg=THEME["text"], anchor="w").grid(row=row, column=0, sticky="ew", padx=12, pady=10)
            scale = ttk.Scale(rules, from_=minimum, to=maximum, variable=var, orient="horizontal", command=lambda _v: self._update_threshold_labels())
            scale.grid(row=row, column=1, sticky="ew", padx=8)
            label = tk.Label(rules, text=f"{var.get()} {suffix}", bg=THEME["window"], fg=THEME["muted"], width=8)
            label.grid(row=row, column=2, padx=8)
            setattr(self, f"threshold_label_{row}", label)
        rules.columnconfigure(1, weight=1)
        ttk.Button(rules, text="SAVE RULES", command=self._save_thresholds).grid(row=5, column=0, columnspan=3, pady=14)
        tk.Label(
            rules,
            text="The engine waits for sustained pressure and then lowers known background apps. It does not kill Windows services or run a fake RAM cleaner.",
            bg=THEME["window"], fg=THEME["muted"], wraplength=470, justify="left",
        ).grid(row=6, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 12))

        games = tk.LabelFrame(
            tab, text=" GAME / WORKLOAD LIST ", bg=THEME["window"], fg=THEME["text"],
            font=("Segoe UI Semibold", 9), bd=1, relief="groove",
        )
        games.grid(row=1, column=1, sticky="nsew", padx=(5, 0))
        self.game_list = tk.Listbox(games, bg="#f8f8f8", fg=THEME["text"], relief="sunken", bd=1, font=("Consolas", 9))
        self.game_list.pack(fill="both", expand=True, padx=10, pady=10)
        game_buttons = tk.Frame(games, bg=THEME["window"])
        game_buttons.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(game_buttons, text="ADD EXE", command=self._add_game).pack(side="left", padx=3)
        ttk.Button(game_buttons, text="ADD RUNNING PROCESS", command=self._add_running_game).pack(side="left", padx=3)
        ttk.Button(game_buttons, text="REMOVE", command=self._remove_game).pack(side="left", padx=3)
        ttk.Button(game_buttons, text="RESET DEFAULTS", command=self._reset_games).pack(side="right", padx=3)
        self._populate_game_list()

    def _build_startup_tab(self) -> None:
        tab = self._new_tab("STARTUP")
        controls = tk.Frame(tab, bg=THEME["window"])
        controls.pack(fill="x", pady=(0, 8))
        self.var_start_windows = tk.BooleanVar(value=WindowsOps.startup_installed())
        ttk.Checkbutton(
            controls, text="Start Helios when I sign in", variable=self.var_start_windows,
            command=self._toggle_startup,
        ).pack(side="left")
        self.var_start_minimized = tk.BooleanVar(value=self.settings.start_minimized)
        ttk.Checkbutton(
            controls, text="Start minimized to tray", variable=self.var_start_minimized,
            command=self._save_general_settings,
        ).pack(side="left", padx=16)
        ttk.Button(controls, text="REFRESH STARTUP LIST", command=self._refresh_startup).pack(side="right", padx=4)
        ttk.Button(controls, text="RESTORE DISABLED", command=self._restore_startup).pack(side="right", padx=4)
        ttk.Button(controls, text="DISABLE SELECTED", command=self._disable_selected_startup).pack(side="right", padx=4)

        columns = ("Name", "Location", "Recommendation", "Command")
        self.startup_tree = ttk.Treeview(tab, columns=columns, show="headings", selectmode="browse")
        self.startup_tree.heading("Name", text="Name")
        self.startup_tree.heading("Location", text="Location")
        self.startup_tree.heading("Recommendation", text="Recommendation")
        self.startup_tree.heading("Command", text="Command")
        self.startup_tree.column("Name", width=190)
        self.startup_tree.column("Location", width=90)
        self.startup_tree.column("Recommendation", width=380)
        self.startup_tree.column("Command", width=480)
        scroll_y = ttk.Scrollbar(tab, orient="vertical", command=self.startup_tree.yview)
        scroll_x = ttk.Scrollbar(tab, orient="horizontal", command=self.startup_tree.xview)
        self.startup_tree.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        self.startup_tree.pack(fill="both", expand=True)
        scroll_y.place(relx=1.0, rely=0.06, relheight=0.88, anchor="ne")
        scroll_x.pack(fill="x")
        self.startup_rows: List[Dict[str, str]] = []

    def _build_stability_tab(self) -> None:
        tab = self._new_tab("STABILITY")
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(1, weight=1)

        findings = tk.LabelFrame(
            tab, text=" THIS PC'S STABILITY FINDINGS ", bg=THEME["window"], fg=THEME["text"],
            font=("Segoe UI Semibold", 9), bd=1, relief="groove",
        )
        findings.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        text = (
            "HP OMEN MAX 16-ah0xxx  |  Core Ultra 9 275HX  |  RTX 5060 Laptop GPU  |  32 GB DDR5-5600\n"
            "Primary freeze clue: Wallpaper Engine crashed inside Intel graphics driver igd9dxva64.dll. "
            "The diagnostic also showed a manually limited 2 GB page file and substantial startup load. "
            "Keep the Razer DeathAdder V3 at 1000 Hz while testing freezes."
        )
        tk.Label(
            findings, text=text, bg=THEME["window"], fg=THEME["text"],
            wraplength=1080, justify="left", anchor="w", padx=12, pady=10,
        ).pack(fill="x")

        repairs = tk.LabelFrame(
            tab, text=" SAFE REPAIRS ", bg=THEME["window"], fg=THEME["text"],
            font=("Segoe UI Semibold", 9), bd=1, relief="groove",
        )
        repairs.grid(row=1, column=0, sticky="nsew", padx=(0, 5))
        self.pagefile_label = tk.Label(
            repairs, text="Page file: checking...", bg=THEME["window"], fg=THEME["text"],
            anchor="w", justify="left",
        )
        self.pagefile_label.pack(fill="x", padx=12, pady=(12, 6))
        ttk.Button(repairs, text="ENABLE WINDOWS-MANAGED PAGE FILE", command=self._fix_pagefile).pack(fill="x", padx=12, pady=4)
        ttk.Button(repairs, text="RESTORE PAGE-FILE BACKUP", command=self._restore_pagefile).pack(fill="x", padx=12, pady=4)
        ttk.Button(repairs, text="CLEAR GRAPHICS SHADER CACHES", command=self._clear_graphics_caches).pack(fill="x", padx=12, pady=4)
        ttk.Button(repairs, text="SAFE TEMP CLEANUP", command=self._safe_cleanup).pack(fill="x", padx=12, pady=4)
        ttk.Button(repairs, text="RUN DISM + SFC IN TERMINAL", command=self._launch_windows_repair).pack(fill="x", padx=12, pady=4)
        ttk.Button(repairs, text="OPEN HP DRIVER SUPPORT", command=lambda: webbrowser.open("https://support.hp.com/drivers")).pack(fill="x", padx=12, pady=4)
        ttk.Button(repairs, text="OPEN INTEL DRIVER SUPPORT", command=lambda: webbrowser.open("https://www.intel.com/content/www/us/en/support/detect.html")).pack(fill="x", padx=12, pady=4)
        ttk.Button(repairs, text="OPEN NVIDIA DRIVER PAGE", command=lambda: webbrowser.open("https://www.nvidia.com/Download/index.aspx")).pack(fill="x", padx=12, pady=4)
        tk.Label(
            repairs,
            text="No overclocking, TDR edits, security disabling, service nuking, or registry tweak packs are used.",
            bg=THEME["window"], fg=THEME["muted"], wraplength=480, justify="left",
        ).pack(fill="x", padx=12, pady=12)

        events = tk.LabelFrame(
            tab, text=" RECENT WINDOWS ERRORS / WARNINGS ", bg=THEME["window"], fg=THEME["text"],
            font=("Segoe UI Semibold", 9), bd=1, relief="groove",
        )
        events.grid(row=1, column=1, sticky="nsew", padx=(5, 0))
        ttk.Button(events, text="SCAN LAST 72 HOURS", command=self._scan_events).pack(anchor="e", padx=8, pady=6)
        self.event_tree = ttk.Treeview(events, columns=("Time", "Level", "ID", "Provider", "Message"), show="headings")
        for column, width in (("Time", 145), ("Level", 70), ("ID", 55), ("Provider", 180), ("Message", 420)):
            self.event_tree.heading(column, text=column)
            self.event_tree.column(column, width=width)
        self.event_tree.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.event_tree.bind("<Double-1>", self._show_event_details)

    def _build_updates_tab(self) -> None:
        tab = self._new_tab("UPDATES")
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(2, weight=1)

        summary = tk.LabelFrame(
            tab, text=" UPDATE STATUS ", bg=THEME["window"], fg=THEME["text"],
            font=("Segoe UI Semibold", 9), bd=1, relief="groove",
        )
        summary.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        self.update_state_label = tk.Label(
            summary,
            text=f"Helios {APP_VERSION} is installed. Configure a public GitHub release repository below.",
            bg=THEME["window"], fg=THEME["text"], font=("Segoe UI Semibold", 10),
            anchor="w", justify="left",
        )
        self.update_state_label.pack(fill="x", padx=12, pady=(10, 5))
        self.update_detail_label = tk.Label(
            summary, text="No update check has run in this session.",
            bg=THEME["window"], fg=THEME["muted"], anchor="w", justify="left",
        )
        self.update_detail_label.pack(fill="x", padx=12, pady=(0, 8))
        self.update_progress = ttk.Progressbar(summary, orient="horizontal", mode="determinate", maximum=100)
        self.update_progress.pack(fill="x", padx=12, pady=(0, 12))

        config = tk.LabelFrame(
            tab, text=" RELEASE CHANNEL ", bg=THEME["window"], fg=THEME["text"],
            font=("Segoe UI Semibold", 9), bd=1, relief="groove",
        )
        config.grid(row=1, column=0, sticky="nsew", padx=(0, 5), pady=(0, 10))
        self.var_update_owner = tk.StringVar(value=self.settings.update_repository_owner)
        self.var_update_repo = tk.StringVar(value=self.settings.update_repository_name)
        self.var_update_channel = tk.StringVar(value=self.settings.update_channel.title())
        self.var_auto_updates = tk.BooleanVar(value=self.settings.automatic_update_checks)
        self.var_auto_download = tk.BooleanVar(value=self.settings.auto_download_updates)
        self.var_update_interval = tk.IntVar(value=self.settings.update_check_interval_hours)

        labels = (("GitHub owner", self.var_update_owner), ("Repository", self.var_update_repo))
        for row, (label, variable) in enumerate(labels):
            tk.Label(config, text=label, bg=THEME["window"], fg=THEME["text"], anchor="w").grid(
                row=row, column=0, sticky="w", padx=12, pady=7,
            )
            ttk.Entry(config, textvariable=variable, width=32).grid(row=row, column=1, sticky="ew", padx=8, pady=7)
        tk.Label(config, text="Channel", bg=THEME["window"], fg=THEME["text"], anchor="w").grid(
            row=2, column=0, sticky="w", padx=12, pady=7,
        )
        ttk.Combobox(
            config, textvariable=self.var_update_channel, values=("Stable", "Beta"),
            state="readonly", width=14,
        ).grid(row=2, column=1, sticky="w", padx=8, pady=7)
        tk.Label(config, text="Check interval", bg=THEME["window"], fg=THEME["text"], anchor="w").grid(
            row=3, column=0, sticky="w", padx=12, pady=7,
        )
        interval_box = tk.Frame(config, bg=THEME["window"])
        interval_box.grid(row=3, column=1, sticky="w", padx=8, pady=7)
        tk.Spinbox(interval_box, from_=1, to=168, textvariable=self.var_update_interval, width=7).pack(side="left")
        tk.Label(interval_box, text="hours", bg=THEME["window"], fg=THEME["muted"]).pack(side="left", padx=6)
        ttk.Checkbutton(
            config, text="Check automatically in the background", variable=self.var_auto_updates,
        ).grid(row=4, column=0, columnspan=2, sticky="w", padx=12, pady=5)
        ttk.Checkbutton(
            config, text="Download verified packages automatically", variable=self.var_auto_download,
        ).grid(row=5, column=0, columnspan=2, sticky="w", padx=12, pady=5)
        config.columnconfigure(1, weight=1)
        config_actions = tk.Frame(config, bg=THEME["window"])
        config_actions.grid(row=6, column=0, columnspan=2, sticky="ew", padx=9, pady=10)
        ttk.Button(config_actions, text="SAVE CHANNEL", command=self._save_update_settings).pack(side="left", padx=3)
        ttk.Button(config_actions, text="OPEN RELEASES", command=self._open_update_releases).pack(side="left", padx=3)

        actions = tk.LabelFrame(
            tab, text=" UPDATE CONTROL ", bg=THEME["window"], fg=THEME["text"],
            font=("Segoe UI Semibold", 9), bd=1, relief="groove",
        )
        actions.grid(row=1, column=1, sticky="nsew", padx=(5, 0), pady=(0, 10))
        self.update_check_button = ttk.Button(actions, text="CHECK NOW", command=lambda: self._check_for_updates(manual=True))
        self.update_check_button.pack(fill="x", padx=12, pady=(12, 4))
        self.update_download_button = ttk.Button(actions, text="DOWNLOAD VERIFIED UPDATE", command=self._download_available_update, state="disabled")
        self.update_download_button.pack(fill="x", padx=12, pady=4)
        self.update_install_button = ttk.Button(actions, text="INSTALL + RESTART HELIOS", command=self._install_staged_update, state="disabled")
        self.update_install_button.pack(fill="x", padx=12, pady=4)
        self.update_skip_button = ttk.Button(actions, text="SKIP THIS VERSION", command=self._skip_available_update, state="disabled")
        self.update_skip_button.pack(fill="x", padx=12, pady=4)
        self.update_rollback_button = ttk.Button(actions, text="ROLL BACK LAST UPDATE", command=self._rollback_last_update, state="disabled")
        self.update_rollback_button.pack(fill="x", padx=12, pady=4)
        self.update_history_label = tk.Label(
            actions, text="No rollback backup detected.", bg=THEME["window"], fg=THEME["muted"],
            justify="left", anchor="w", wraplength=500,
        )
        self.update_history_label.pack(fill="x", padx=12, pady=12)

        notes = tk.LabelFrame(
            tab, text=" RELEASE NOTES / UPDATE LOG ", bg=THEME["window"], fg=THEME["text"],
            font=("Segoe UI Semibold", 9), bd=1, relief="groove",
        )
        notes.grid(row=2, column=0, columnspan=2, sticky="nsew")
        self.update_notes_text = tk.Text(
            notes, bg="#f8f8f8", fg=THEME["text"], font=("Segoe UI", 9),
            relief="sunken", bd=1, wrap="word", state="disabled",
        )
        self.update_notes_text.pack(fill="both", expand=True, padx=8, pady=8)
        self._set_text(
            self.update_notes_text,
            "Helios releases are delivered through GitHub assets. The hub verifies SHA-256, requires the configured Ed25519 release signature, validates every archive path, stages the build, backs up the current installation, and only then restarts into the update.",
        )
        self.after(800, self._refresh_update_history)

    def _build_logs_tab(self) -> None:
        tab = self._new_tab("LOGS")
        controls = tk.Frame(tab, bg=THEME["window"])
        controls.pack(fill="x", pady=(0, 8))
        ttk.Button(controls, text="SAVE COPY", command=self._save_log_copy).pack(side="left", padx=4)
        ttk.Button(controls, text="OPEN LOG FOLDER", command=lambda: self._open_folder(PATHS.logs)).pack(side="left", padx=4)
        ttk.Button(controls, text="CLEAR VIEW", command=self._clear_log_view).pack(side="left", padx=4)
        ttk.Button(controls, text="OPEN DATA FOLDER", command=lambda: self._open_folder(PATHS.data)).pack(side="right", padx=4)
        self.log_text = tk.Text(
            tab, bg="#161616", fg="#ededed", insertbackground="#ffffff",
            font=("Consolas", 9), wrap="none", relief="sunken", bd=1,
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

    def _queue_log_line(self, line: str) -> None:
        try:
            self.output_queue.put_nowait(("log", line))
        except Exception:
            pass

    def _poll_queue(self) -> None:
        processed = 0
        while processed < 60:
            try:
                kind, payload = self.output_queue.get_nowait()
            except queue.Empty:
                break
            processed += 1
            if kind == "snapshot":
                self.latest_snapshot = payload
                self.current_assessment = self.health_analyzer.assess(payload)
                if self.settings.telemetry_enabled:
                    self.telemetry.record_snapshot(payload, self.current_assessment)
                self._render_snapshot(payload)
            elif kind == "processes":
                self.latest_processes = payload
                self._populate_process_tree()
            elif kind == "monitor_error":
                self.engine_status.configure(text=f"ENGINE ERROR: {payload}", fg=THEME["bad"])
            elif kind == "log":
                self._append_log(payload)
            elif kind == "startup_rows":
                self._render_startup_rows(payload)
            elif kind == "events":
                self._render_events(payload)
            elif kind == "update_status":
                self._render_update_status(payload)
            elif kind == "update_progress":
                completed, total = payload
                self._render_update_progress(completed, total)
            elif kind == "update_check_result":
                self._handle_update_check_result(payload)
            elif kind == "update_stage_result":
                self._handle_update_stage_result(payload)
            elif kind == "task_message":
                title, message, is_error = payload
                if is_error:
                    messagebox.showerror(title, message)
                else:
                    messagebox.showinfo(title, message)
        if not self._shutting_down:
            self.after(150, self._poll_queue)

    def _render_snapshot(self, snap: Snapshot) -> None:
        self.clock_status.configure(text=dt.datetime.fromtimestamp(snap.timestamp).strftime("%H:%M:%S"))
        engine_text = f"ENGINE: {self.optimizer.status_text.upper()}"
        self.engine_status.configure(text=engine_text, fg=THEME["good"] if not self.optimizer.paused else THEME["warn"])
        self.foreground_status.configure(text=f"FOREGROUND: {snap.foreground_name or '--'}")
        self.power_status.configure(text=f"POWER: {snap.active_power_plan}")

        self.metric_cpu.set(f"{snap.cpu_percent:.0f}%", snap.cpu_percent, f"{snap.cpu_frequency_mhz:.0f} MHz")
        self.metric_ram.set(
            f"{snap.memory_percent:.0f}%",
            snap.memory_percent,
            f"{human_bytes(snap.memory_used)} / {human_bytes(snap.memory_total)}",
        )
        gpu_percent = snap.gpu_percent or 0.0
        gpu_detail = "Telemetry unavailable"
        if snap.gpu_memory_used_mb is not None and snap.gpu_memory_total_mb is not None:
            temp = f"  |  {snap.gpu_temperature_c:.0f} C" if snap.gpu_temperature_c is not None else ""
            gpu_detail = f"{snap.gpu_memory_used_mb:.0f}/{snap.gpu_memory_total_mb:.0f} MB{temp}"
        self.metric_gpu.set(f"{gpu_percent:.0f}%" if snap.gpu_percent is not None else "--", gpu_percent, gpu_detail)
        self.metric_disk.set(
            f"{snap.disk_percent:.0f}%",
            snap.disk_percent,
            f"R {human_rate(snap.disk_read_rate)}  W {human_rate(snap.disk_write_rate)}",
        )
        assessment = self.current_assessment
        health_color = THEME["good"] if assessment.score >= 75 else THEME["warn"] if assessment.score >= 55 else THEME["bad"]
        if hasattr(self, "health_score_label"):
            self.health_score_label.configure(text=f"{assessment.score} / 100", fg=health_color)
            self.health_summary_label.configure(text=f"{assessment.grade}: {assessment.summary}")

        battery_text = "N/A"
        if snap.battery_percent is not None:
            battery_text = f"{snap.battery_percent:.0f}% ({'plugged in' if snap.battery_plugged else 'battery'})"
        details = (
            f"Computer: {platform.node()}\n"
            f"Operating system: {platform.platform()}\n"
            f"Processor: {platform.processor() or 'Intel Core Ultra 9 275HX'}\n"
            f"GPU: {snap.gpu_name}\n"
            f"Processes / threads / handles: {snap.process_count} / {snap.thread_count} / {snap.handle_count}\n"
            f"Network: down {human_rate(snap.network_recv_rate)}  up {human_rate(snap.network_send_rate)}\n"
            f"Virtual memory use: {snap.swap_percent:.1f}%\n"
            f"Battery: {battery_text}\n"
            f"Foreground process: {snap.foreground_name or 'Unknown'} ({snap.foreground_pid or '--'})\n"
            f"Active power plan: {snap.active_power_plan}\n"
            f"Admin mode: {'Yes' if is_admin() else 'No'}"
        )
        self._set_text(self.live_details, details)
        self._set_text(self.analysis_text, self._analysis_for_snapshot(snap))

    def _analysis_for_snapshot(self, snap: Snapshot) -> str:
        lines: List[str] = []
        score = 100
        if snap.cpu_percent >= 90:
            lines.append("CPU is saturated. Helios will lower known background-process priority after the sustained-pressure timer expires.")
            score -= 18
        elif snap.cpu_percent >= 70:
            lines.append("CPU load is elevated, but not yet critical.")
            score -= 7
        else:
            lines.append("CPU headroom is healthy.")

        if snap.memory_percent >= self.settings.memory_pressure_threshold:
            lines.append("Memory pressure is high. Do not use a RAM cleaner; close genuinely unused applications or let Helios deprioritize known background apps.")
            score -= 18
        elif snap.memory_percent >= 75:
            lines.append("Memory usage is moderately high.")
            score -= 7
        else:
            lines.append("Memory headroom is healthy for a 32 GB system.")

        if snap.gpu_temperature_c is not None:
            if snap.gpu_temperature_c >= 87:
                lines.append("NVIDIA GPU temperature is very high. Check OMEN fan mode, vents, and HWiNFO sensor logging.")
                score -= 20
            elif snap.gpu_temperature_c >= 80:
                lines.append("NVIDIA GPU is warm under load; watch for throttling.")
                score -= 8
            else:
                lines.append("NVIDIA GPU temperature is within a normal operating range for the current snapshot.")
        else:
            lines.append("NVIDIA temperature telemetry was unavailable; HWiNFO remains the best freeze-test sensor logger.")

        if snap.foreground_name.lower() in {"wallpaper64.exe", "wallpaper32.exe", "wallpaperui.exe"}:
            lines.append("Wallpaper Engine is active. This machine previously logged a Wallpaper Engine crash inside the Intel graphics driver.")
            score -= 15
        if "balanced" in snap.active_power_plan.lower() and self.settings.profile in {"Gaming", "Maximum Performance"}:
            lines.append("The selected Helios profile expects a performance power plan, but Windows reports Balanced.")
            score -= 8

        score = int(clamp(score, 0, 100))
        grade = "READY" if score >= 85 else "WATCH" if score >= 65 else "CONSTRAINED"
        return f"READINESS: {score}/100  /  {grade}\n\n" + "\n\n".join(lines)

    @staticmethod
    def _set_text(widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def _append_log(self, line: str) -> None:
        self._log_lines.append(line)
        if len(self._log_lines) > 4000:
            self._log_lines = self._log_lines[-3000:]
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _initial_refresh(self) -> None:
        self._refresh_startup()
        self._refresh_pagefile()

    def _manual_refresh(self) -> None:
        self._refresh_processes()
        self._refresh_startup()
        self._refresh_pagefile()
        LOGGER.log("Manual refresh requested.")

    def _refresh_processes(self) -> None:
        def task() -> None:
            rows = ResourceSampler.process_rows()
            self.output_queue.put(("processes", rows))
        threading.Thread(target=task, name="ProcessRefresh", daemon=True).start()

    def _sort_processes(self, column: str) -> None:
        if self.process_sort_column == column:
            self.process_sort_reverse = not self.process_sort_reverse
        else:
            self.process_sort_column = column
            self.process_sort_reverse = column in {"CPU %", "Memory MB", "PID", "Threads", "Handles"}
        self._populate_process_tree()

    def _populate_process_tree(self) -> None:
        if not hasattr(self, "process_tree"):
            return
        filter_text = self.process_filter.get().strip().lower()
        rows = [row for row in self.latest_processes if not filter_text or filter_text in row.name.lower() or filter_text in str(row.pid)]
        key_map: Dict[str, Callable[[ProcessRow], Any]] = {
            "PID": lambda r: r.pid,
            "Name": lambda r: r.name.lower(),
            "CPU %": lambda r: r.cpu,
            "Memory MB": lambda r: r.memory_mb,
            "Priority": lambda r: r.priority,
            "Threads": lambda r: r.threads,
            "Handles": lambda r: r.handles,
            "Status": lambda r: r.status,
            "User": lambda r: r.username,
        }
        rows.sort(key=key_map.get(self.process_sort_column, lambda r: r.cpu), reverse=self.process_sort_reverse)
        selected_pid = self._selected_process_pid()
        self.process_tree.delete(*self.process_tree.get_children())
        for row in rows[:700]:
            item = self.process_tree.insert(
                "",
                "end",
                iid=f"pid-{row.pid}",
                values=(row.pid, row.name, f"{row.cpu:.1f}", f"{row.memory_mb:.1f}", row.priority, row.threads, row.handles, row.status, row.username),
            )
            if selected_pid == row.pid:
                self.process_tree.selection_set(item)

    def _selected_process_pid(self) -> Optional[int]:
        if not hasattr(self, "process_tree"):
            return None
        selected = self.process_tree.selection()
        if not selected:
            return None
        try:
            return int(self.process_tree.item(selected[0], "values")[0])
        except Exception:
            return None

    def _selected_priority(self, target: str) -> None:
        pid = self._selected_process_pid()
        if pid is None:
            messagebox.showinfo("Select a process", "Select a process first.")
            return
        ok, message = set_process_priority(pid, target)
        LOGGER.log(message, "ACTION" if ok else "WARN")
        if not ok:
            messagebox.showerror("Priority change", message)
        self._refresh_processes()

    def _manual_deprioritize(self) -> None:
        count = self.optimizer._deprioritize_background(exclude_pid=None, reason="manual request")
        messagebox.showinfo("Background management", f"Adjusted {count} known background process(es).")
        self._refresh_processes()

    def _end_selected_process(self) -> None:
        pid = self._selected_process_pid()
        if pid is None or psutil is None:
            messagebox.showinfo("Select a process", "Select a process first.")
            return
        try:
            proc = psutil.Process(pid)
            name = proc.name()
            if name.lower() in PROTECTED_PROCESS_NAMES:
                messagebox.showerror("Protected process", f"{name} is protected and cannot be ended from Helios.")
                return
            if not messagebox.askyesno("End process", f"End {name} ({pid})? Unsaved work in that app may be lost."):
                return
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except psutil.TimeoutExpired:
                if messagebox.askyesno("Process did not close", f"{name} did not exit. Force-kill it?"):
                    proc.kill()
            LOGGER.log(f"User ended {name} ({pid}).", "ACTION")
        except Exception as exc:
            messagebox.showerror("End process failed", str(exc))
        self._refresh_processes()

    def _show_process_details(self, _event: Any = None) -> None:
        pid = self._selected_process_pid()
        if pid is None:
            return
        row = next((item for item in self.latest_processes if item.pid == pid), None)
        if not row:
            return
        messagebox.showinfo(
            f"{row.name} ({row.pid})",
            f"CPU: {row.cpu:.1f}%\nMemory: {row.memory_mb:.1f} MB\nPriority: {row.priority}\n"
            f"Threads: {row.threads}\nHandles: {row.handles}\nStatus: {row.status}\nUser: {row.username}\n\nExecutable:\n{row.executable or 'Unavailable'}",
        )

    def _apply_profile(self, profile: str) -> None:
        ok, message = self.optimizer.apply_profile(profile)
        if ok:
            messagebox.showinfo("Profile applied", message)
        else:
            messagebox.showerror("Profile failed", message)

    def _save_optimizer_settings(self) -> None:
        self.settings.optimizer_enabled = self.var_optimizer.get()
        self.settings.auto_game_detection = self.var_game_detect.get()
        self.settings.lower_background_priority = self.var_lower_cpu.get()
        self.settings.lower_background_io = self.var_lower_io.get()
        self.settings.stop_wallpaper_during_games = self.var_stop_wallpaper.get()
        self.settings.close_approved_apps_during_games = self.var_close_approved.get()
        self.settings.restore_after_game = self.var_restore.get()
        self.settings.save()
        self.optimizer.update_settings(self.settings)
        LOGGER.log("Automatic optimizer settings updated.")

    def _update_threshold_labels(self) -> None:
        values = [self.cpu_threshold.get(), self.ram_threshold.get(), self.disk_threshold.get(), self.pressure_seconds.get()]
        suffixes = ["%", "%", "%", "sec"]
        for index, (value, suffix) in enumerate(zip(values, suffixes)):
            label = getattr(self, f"threshold_label_{index}", None)
            if label:
                label.configure(text=f"{int(value)} {suffix}")

    def _save_thresholds(self) -> None:
        self.settings.cpu_pressure_threshold = int(self.cpu_threshold.get())
        self.settings.memory_pressure_threshold = int(self.ram_threshold.get())
        self.settings.disk_pressure_threshold = int(self.disk_threshold.get())
        self.settings.pressure_seconds = int(self.pressure_seconds.get())
        self.settings.save()
        self.optimizer.update_settings(self.settings)
        LOGGER.log("Pressure rules updated.")
        messagebox.showinfo("Rules saved", "Automatic pressure rules were saved.")

    def _populate_game_list(self) -> None:
        self.game_list.delete(0, "end")
        for name in sorted(self.settings.game_executables):
            self.game_list.insert("end", name)

    def _add_game(self) -> None:
        path = filedialog.askopenfilename(title="Select a game or workload executable", filetypes=[("Windows executable", "*.exe"), ("All files", "*.*")])
        if not path:
            return
        name = Path(path).name.lower()
        if name not in self.settings.game_executables:
            self.settings.game_executables.append(name)
            self.settings.game_executables.sort()
            self.settings.save()
            self._populate_game_list()
            LOGGER.log(f"Added game detection rule: {name}")

    def _add_running_game(self) -> None:
        pid = self._selected_process_pid()
        if pid is None:
            messagebox.showinfo("Processes tab", "Select a process in the Processes tab, then return here and click this button.")
            return
        row = next((item for item in self.latest_processes if item.pid == pid), None)
        if row and row.name.lower() not in self.settings.game_executables:
            self.settings.game_executables.append(row.name.lower())
            self.settings.game_executables.sort()
            self.settings.save()
            self._populate_game_list()

    def _remove_game(self) -> None:
        selected = self.game_list.curselection()
        if not selected:
            return
        name = self.game_list.get(selected[0])
        self.settings.game_executables = [item for item in self.settings.game_executables if item != name]
        self.settings.save()
        self._populate_game_list()

    def _reset_games(self) -> None:
        self.settings.game_executables = sorted(COMMON_GAME_EXECUTABLES)
        self.settings.save()
        self._populate_game_list()

    def _toggle_startup(self) -> None:
        enabled = self.var_start_windows.get()
        ok, message = WindowsOps.install_startup(enabled)
        if ok:
            self.settings.start_with_windows = enabled
            self.settings.save()
            LOGGER.log(message, "ACTION")
        else:
            self.var_start_windows.set(not enabled)
            messagebox.showerror("Startup setting", message)

    def _save_general_settings(self) -> None:
        self.settings.start_minimized = self.var_start_minimized.get()
        self.settings.save()

    def _save_update_settings(self, show_message: bool = True) -> None:
        owner = self.var_update_owner.get().strip()
        repository = self.var_update_repo.get().strip() or "helios-performance-hub"
        if owner and not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", owner):
            messagebox.showerror("Update channel", "The GitHub owner contains invalid characters.")
            return
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository):
            messagebox.showerror("Update channel", "The repository name contains invalid characters.")
            return
        try:
            interval = max(1, min(168, int(self.var_update_interval.get())))
        except Exception:
            interval = 6
            self.var_update_interval.set(interval)
        channel = self.var_update_channel.get().strip().lower()
        if channel not in {"stable", "beta"}:
            channel = "stable"
            self.var_update_channel.set("Stable")

        self.settings.update_repository_owner = owner
        self.settings.update_repository_name = repository
        self.settings.update_channel = channel
        self.settings.automatic_update_checks = bool(self.var_auto_updates.get())
        self.settings.auto_download_updates = bool(self.var_auto_download.get())
        self.settings.update_check_interval_hours = interval
        self.settings.save()
        LOGGER.log(f"Update channel saved: {owner or '[not configured]'}/{repository} ({channel}).")
        if show_message:
            messagebox.showinfo(
                "Update channel saved",
                "The release channel was saved. A public GitHub repository is required for unattended checks.",
            )

    def _set_update_busy(self, busy: bool) -> None:
        self.update_busy = busy
        self.update_check_button.configure(state="disabled" if busy else "normal")
        self.update_download_button.configure(
            state="disabled" if busy or self.available_update is None or self.staged_update is not None else "normal"
        )
        self.update_install_button.configure(state="normal" if not busy and self.staged_update is not None else "disabled")
        self.update_skip_button.configure(state="normal" if not busy and self.available_update is not None else "disabled")
        rollback = self.update_manager.rollback_info()
        self.update_rollback_button.configure(state="normal" if not busy and rollback is not None else "disabled")

    def _render_update_status(self, payload: Any) -> None:
        if isinstance(payload, tuple):
            text = str(payload[0])
            level = str(payload[1]) if len(payload) > 1 else "info"
            detail = str(payload[2]) if len(payload) > 2 else ""
        else:
            text, level, detail = str(payload), "info", ""
        color = THEME["bad"] if level == "error" else THEME["warn"] if level == "warn" else THEME["good"] if level == "good" else THEME["text"]
        self.update_state_label.configure(text=text, fg=color)
        if detail:
            self.update_detail_label.configure(text=detail)

    def _render_update_progress(self, completed: int, total: int) -> None:
        if total > 0:
            percent = clamp((completed / total) * 100.0, 0.0, 100.0)
            self.update_progress.configure(mode="determinate", value=percent)
            self.update_detail_label.configure(
                text=f"Downloaded {human_bytes(completed)} of {human_bytes(total)}  /  {percent:.0f}%"
            )
        else:
            self.update_progress.configure(mode="indeterminate")
            try:
                self.update_progress.start(12)
            except Exception:
                pass
            self.update_detail_label.configure(text=f"Downloaded {human_bytes(completed)}")

    def _startup_update_check(self) -> None:
        try:
            self.update_manager.clear_stale_downloads()
        except Exception as exc:
            LOGGER.log(f"Update-cache cleanup failed: {exc}", "WARN")
        self._refresh_update_history()
        self._auto_update_tick()

    def _auto_update_tick(self) -> None:
        if self._shutting_down:
            return
        try:
            configured = bool(self.settings.update_repository_owner and self.settings.update_repository_name)
            due_after = max(1, self.settings.update_check_interval_hours) * 3600
            due = time.time() - float(self.settings.last_update_check_epoch or 0.0) >= due_after
            if configured and due and self.settings.automatic_update_checks and not self.update_busy:
                self._check_for_updates(manual=False)
        except Exception as exc:
            LOGGER.log(f"Automatic update scheduling failed: {exc}", "WARN")
        self.after(30 * 60 * 1000, self._auto_update_tick)

    def _check_for_updates(self, manual: bool = False) -> None:
        if self.update_busy:
            if manual:
                messagebox.showinfo("Updates", "An update operation is already running.")
            return
        self._save_update_settings(show_message=False)
        owner = self.settings.update_repository_owner.strip()
        repository = self.settings.update_repository_name.strip()
        if not owner:
            self._render_update_status(
                ("UPDATE CHANNEL NOT CONFIGURED", "warn", "Enter the GitHub owner that will publish Helios releases, then save the channel."),
            )
            if manual:
                messagebox.showinfo(
                    "Configure updates",
                    "Enter the GitHub owner and repository in the Updates tab first. The included publisher tools can create release packages for that repository.",
                )
            return

        self._set_update_busy(True)
        self.update_progress.stop()
        self.update_progress.configure(mode="indeterminate", value=0)
        self.update_progress.start(12)
        self._render_update_status(("CHECKING FOR UPDATES", "info", f"Reading {owner}/{repository}  /  {self.settings.update_channel.title()} channel"))

        def task() -> None:
            try:
                release = self.update_manager.check(owner, repository, self.settings.update_channel)
                self.output_queue.put(("update_check_result", (release, manual, "")))
            except Exception as exc:
                self.output_queue.put(("update_check_result", (None, manual, str(exc))))

        threading.Thread(target=task, name="HeliosUpdateCheck", daemon=True).start()

    def _handle_update_check_result(self, payload: Any) -> None:
        release, manual, error = payload
        self.update_progress.stop()
        self.update_progress.configure(mode="determinate", value=0)
        if not error:
            self.settings.last_update_check_epoch = time.time()
            self.settings.save()
        self._set_update_busy(False)

        if error:
            LOGGER.log(f"Update check failed: {error}", "WARN")
            self._render_update_status(("UPDATE CHECK FAILED", "error", error))
            if manual:
                messagebox.showerror("Update check failed", error)
            return

        if release is None:
            self.available_update = None
            self.staged_update = None
            self._set_update_busy(False)
            self._render_update_status(
                (f"HELIOS {APP_VERSION} IS UP TO DATE", "good", f"Last checked {now_text()}  /  {self.settings.update_channel.title()} channel"),
            )
            if manual:
                messagebox.showinfo("Helios is current", f"No release newer than {APP_VERSION} was found.")
            return

        self.available_update = release
        self.staged_update = None
        skipped = release.version == self.settings.skipped_update_version
        state = "UPDATE AVAILABLE — SKIPPED" if skipped and not manual else "UPDATE AVAILABLE"
        level = "warn" if skipped and not manual else "good"
        self._render_update_status(
            (f"{state}: HELIOS {release.version}", level, f"Published {release.published_at or 'date unavailable'}  /  Package {human_bytes(release.package_size)}"),
        )
        self._set_text(
            self.update_notes_text,
            f"{release.title}\nVersion {release.version}\nChannel: {'Beta' if release.prerelease else 'Stable'}\n\n{release.notes}",
        )
        self._set_update_busy(False)
        if self.settings.auto_download_updates and not skipped:
            self.after(350, lambda: self._download_available_update(automatic=True))
        elif manual and skipped:
            messagebox.showinfo("Skipped update", f"Helios {release.version} is available, but this version is currently marked as skipped.")

    def _download_available_update(self, automatic: bool = False) -> None:
        release = self.available_update
        if release is None or self.update_busy:
            return
        self.update_download_was_automatic = automatic
        self._set_update_busy(True)
        self.update_progress.stop()
        self.update_progress.configure(mode="determinate", value=0)
        self._render_update_status((f"DOWNLOADING HELIOS {release.version}", "info", "Downloading and validating the release package."))

        def progress(completed: int, total: int) -> None:
            try:
                self.output_queue.put_nowait(("update_progress", (completed, total)))
            except Exception:
                pass

        def task() -> None:
            try:
                staged = self.update_manager.download_and_stage(release, progress)
                self.output_queue.put(("update_stage_result", (staged, "")))
            except Exception as exc:
                self.output_queue.put(("update_stage_result", (None, str(exc))))

        threading.Thread(target=task, name="HeliosUpdateDownload", daemon=True).start()

    def _handle_update_stage_result(self, payload: Any) -> None:
        staged, error = payload
        self.update_progress.stop()
        self.update_progress.configure(mode="determinate")
        self._set_update_busy(False)
        if error:
            LOGGER.log(f"Update download/staging failed: {error}", "WARN")
            self.update_progress.configure(value=0)
            self._render_update_status(("UPDATE VALIDATION FAILED", "error", error))
            messagebox.showerror("Update failed", error)
            return
        self.staged_update = staged
        self.update_progress.configure(value=100)
        self._render_update_status(
            (f"HELIOS {staged.release.version} IS VERIFIED AND READY", "good", "The current installation will be backed up before any files are replaced."),
        )
        self._set_update_busy(False)
        if not self.update_download_was_automatic:
            messagebox.showinfo(
                "Update ready",
                f"Helios {staged.release.version} passed its integrity, signature, and archive safety checks. Click Install + Restart Helios when ready.",
            )

    def _install_staged_update(self) -> None:
        staged = self.staged_update
        if staged is None or self.update_busy:
            return
        if not messagebox.askyesno(
            "Install Helios update",
            f"Install Helios {staged.release.version} now?\n\nHelios will close, back up version {APP_VERSION}, replace the program files, and restart automatically.",
        ):
            return
        try:
            self.update_manager.begin_install(staged, os.getpid())
            self._render_update_status(("INSTALLER STARTED", "good", "Helios is closing so the verified update can be applied."))
            self.after(150, self.shutdown)
        except Exception as exc:
            LOGGER.log(f"Could not launch updater: {exc}", "ERROR")
            messagebox.showerror("Could not install update", str(exc))

    def _skip_available_update(self) -> None:
        if self.available_update is None:
            return
        self.settings.skipped_update_version = self.available_update.version
        self.settings.save()
        LOGGER.log(f"User skipped Helios {self.available_update.version}.")
        self._render_update_status(
            (f"HELIOS {self.available_update.version} SKIPPED", "warn", "Manual checks will continue to show it; automatic download is suppressed for this version."),
        )

    def _rollback_last_update(self) -> None:
        info = self.update_manager.rollback_info()
        if info is None:
            messagebox.showinfo("Rollback", "No valid previous-version backup is available.")
            self._refresh_update_history()
            return
        if not messagebox.askyesno(
            "Roll back Helios",
            f"Replace Helios {info.version_after} with the backed-up {info.version_before} installation?\n\nThe current program files will be preserved separately for diagnosis.",
        ):
            return
        try:
            self.update_manager.begin_rollback(os.getpid())
            self.after(150, self.shutdown)
        except Exception as exc:
            LOGGER.log(f"Could not launch rollback: {exc}", "ERROR")
            messagebox.showerror("Rollback failed", str(exc))

    def _refresh_update_history(self) -> None:
        if not hasattr(self, "update_history_label"):
            return
        info = self.update_manager.rollback_info()
        worker = self.update_manager.read_worker_status()
        if info:
            text = (
                f"Rollback available: {info.version_after} → {info.version_before}\n"
                f"Installed: {info.installed_at or 'unknown'}\nBackup: {info.backup_path}"
            )
            self.update_rollback_button.configure(state="normal" if not self.update_busy else "disabled")
        else:
            text = "No rollback backup detected. A backup is created automatically before the first in-app update."
            self.update_rollback_button.configure(state="disabled")
        if worker:
            text += f"\n\nLast updater state: {worker.get('state', 'unknown')} — {worker.get('message', '')}"
        self.update_history_label.configure(text=text)

    def _open_update_releases(self) -> None:
        owner = self.var_update_owner.get().strip()
        repository = self.var_update_repo.get().strip()
        if not owner or not repository:
            messagebox.showinfo("Release repository", "Configure and save the GitHub owner and repository first.")
            return
        webbrowser.open(f"https://github.com/{owner}/{repository}/releases")

    def _mark_update_healthy(self) -> None:
        if self._shutting_down:
            return
        try:
            self.update_manager.mark_healthy()
            if "--post-update" in sys.argv:
                LOGGER.log(f"Helios {APP_VERSION} passed its post-update startup health check.", "ACTION")
            if "--rollback-complete" in sys.argv:
                LOGGER.log(f"Helios rollback completed; running version {APP_VERSION}.", "ACTION")
            self._refresh_update_history()
        except Exception as exc:
            LOGGER.log(f"Could not record update health: {exc}", "WARN")

    def _refresh_startup(self) -> None:
        def task() -> None:
            rows = WindowsOps.startup_entries()
            self.output_queue.put(("startup_rows", rows))
        threading.Thread(target=task, name="StartupRefresh", daemon=True).start()

    def _render_startup_rows(self, rows: List[Dict[str, str]]) -> None:
        self.startup_rows = rows
        self.startup_tree.delete(*self.startup_tree.get_children())
        for index, row in enumerate(rows):
            self.startup_tree.insert(
                "", "end", iid=f"startup-{index}",
                values=(row["name"], row["hive"], row["recommendation"], row["command"]),
            )

    def _selected_startup_row(self) -> Optional[Dict[str, str]]:
        selected = self.startup_tree.selection()
        if not selected:
            return None
        try:
            index = int(selected[0].split("-")[-1])
            return self.startup_rows[index]
        except Exception:
            return None

    def _disable_selected_startup(self) -> None:
        row = self._selected_startup_row()
        if not row:
            messagebox.showinfo("Select startup entry", "Select an entry first.")
            return
        if row["name"] == APP_REGISTRY_NAME:
            messagebox.showinfo("Use the Helios toggle", "Use the Start Helios checkbox to change this entry.")
            return
        if not messagebox.askyesno("Disable startup entry", f'Disable "{row["name"]}" at login? The command will be backed up first.'):
            return
        ok, message = WindowsOps.disable_startup_entry(row)
        LOGGER.log(message, "ACTION" if ok else "WARN")
        if not ok:
            if "Administrator" in message and not is_admin():
                message += "\n\nUse RUN AS ADMIN at the top of Helios and retry."
            messagebox.showerror("Startup change", message)
        self._refresh_startup()

    def _restore_startup(self) -> None:
        restored, errors = WindowsOps.restore_startup_entries()
        LOGGER.log(f"Restored {restored} startup entry/entries.", "ACTION")
        message = f"Restored {restored} startup entry/entries."
        if errors:
            message += "\n\n" + "\n".join(errors[:10])
        messagebox.showinfo("Startup restore", message)
        self._refresh_startup()

    def _refresh_pagefile(self) -> None:
        def task() -> None:
            status = WindowsOps.pagefile_status()
            self.after(0, lambda: self.pagefile_label.configure(text=f'Page file: {status["description"]}'))
        threading.Thread(target=task, name="PagefileRefresh", daemon=True).start()

    def _fix_pagefile(self) -> None:
        if not is_admin():
            if messagebox.askyesno("Administrator required", "This repair needs administrator permission. Relaunch Helios as administrator now?"):
                self._relaunch_admin()
            return
        if not messagebox.askyesno("Windows-managed virtual memory", "Enable Windows-managed page-file sizing? The current setting is backed up. A restart is required."):
            return
        def task() -> None:
            ok, message = WindowsOps.enable_automatic_pagefile()
            LOGGER.log(message, "ACTION" if ok else "ERROR")
            self.output_queue.put(("task_message", ("Page-file repair", message, not ok)))
            self.after(0, self._refresh_pagefile)
        threading.Thread(target=task, name="PagefileRepair", daemon=True).start()

    def _restore_pagefile(self) -> None:
        if not is_admin():
            if messagebox.askyesno("Administrator required", "Restoring the page file needs administrator permission. Relaunch Helios as administrator now?"):
                self._relaunch_admin()
            return
        if not messagebox.askyesno("Restore page file", "Restore the page-file configuration saved before Helios enabled Windows-managed sizing? A restart is required."):
            return
        def task() -> None:
            ok, message = WindowsOps.restore_pagefile_backup()
            LOGGER.log(message, "ACTION" if ok else "ERROR")
            self.output_queue.put(("task_message", ("Page-file restore", message, not ok)))
            self.after(0, self._refresh_pagefile)
        threading.Thread(target=task, name="PagefileRestore", daemon=True).start()

    def _clear_graphics_caches(self) -> None:
        if not messagebox.askyesno("Clear graphics caches", "Close games first. Clear DirectX, NVIDIA, and Intel shader caches? They rebuild automatically."):
            return
        def task() -> None:
            removed, errors = WindowsOps.clear_graphics_caches()
            message = f"Removed {removed} cache item(s)."
            if errors:
                message += f"\n{len(errors)} item(s) were in use or inaccessible."
            LOGGER.log(message, "ACTION")
            self.output_queue.put(("task_message", ("Graphics caches", message, False)))
        threading.Thread(target=task, name="CacheCleanup", daemon=True).start()

    def _safe_cleanup(self) -> None:
        if not messagebox.askyesno("Safe cleanup", "Delete files from Windows user temporary folders? Files currently in use are skipped."):
            return
        def task() -> None:
            files, dirs, errors = WindowsOps.safe_temp_cleanup()
            message = f"Removed {files} temporary file(s) and {dirs} temporary folder(s)."
            if errors:
                message += f"\n{len(errors)} item(s) were in use or inaccessible."
            LOGGER.log(message, "ACTION")
            self.output_queue.put(("task_message", ("Safe cleanup", message, False)))
        threading.Thread(target=task, name="TempCleanup", daemon=True).start()

    def _launch_windows_repair(self) -> None:
        if not IS_WINDOWS:
            return
        command = "DISM /Online /Cleanup-Image /RestoreHealth & sfc /scannow & echo. & echo Repair finished. & pause"
        try:
            params = f'/k "{command}"'
            result = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", params, None, 1)
            if result <= 32:
                raise RuntimeError(f"Windows returned code {result}")
            LOGGER.log("Launched elevated DISM and SFC terminal.", "ACTION")
        except Exception as exc:
            messagebox.showerror("Windows repair", str(exc))

    def _scan_events(self) -> None:
        self.engine_status.configure(text="ENGINE: SCANNING WINDOWS EVENTS", fg=THEME["warn"])
        def task() -> None:
            rows = WindowsOps.event_log_summary(72)
            self.output_queue.put(("events", rows))
        threading.Thread(target=task, name="EventScan", daemon=True).start()

    def _render_events(self, rows: List[Dict[str, str]]) -> None:
        self.event_rows = rows
        self.event_tree.delete(*self.event_tree.get_children())
        for index, row in enumerate(rows):
            self.event_tree.insert(
                "", "end", iid=f"event-{index}",
                values=(row["time"], row["level"], row["id"], row["provider"], row["message"]),
            )
        self.engine_status.configure(text=f"ENGINE: {self.optimizer.status_text.upper()}", fg=THEME["good"])
        LOGGER.log(f"Scanned Windows event logs and found {len(rows)} recent error/warning entries.")

    def _show_event_details(self, _event: Any = None) -> None:
        selected = self.event_tree.selection()
        if not selected:
            return
        try:
            row = self.event_rows[int(selected[0].split("-")[-1])]
        except Exception:
            return
        messagebox.showinfo(
            f'{row["provider"]} / Event {row["id"]}',
            f'Time: {row["time"]}\nLevel: {row["level"]}\nProvider: {row["provider"]}\n\n{row["message"]}',
        )

    def _export_snapshot(self) -> None:
        if not self.latest_snapshot:
            messagebox.showinfo("Snapshot", "No monitor snapshot is available yet.")
            return
        default = PATHS.exports / f"helios_snapshot_{compact_stamp()}.json"
        path = filedialog.asksaveasfilename(
            title="Save Helios snapshot",
            initialfile=default.name,
            initialdir=str(default.parent),
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        payload = {
            "app": {"name": APP_NAME, "version": APP_VERSION, "admin": is_admin()},
            "system": {
                "platform": platform.platform(),
                "processor": platform.processor(),
                "computer": platform.node(),
            },
            "snapshot": asdict(self.latest_snapshot),
            "settings": asdict(self.settings),
            "top_processes": [asdict(row) for row in self.latest_processes[:50]],
            "pagefile": WindowsOps.pagefile_status(),
            "startup": WindowsOps.startup_entries(),
        }
        Path(path).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        LOGGER.log(f"Exported snapshot: {path}")
        messagebox.showinfo("Snapshot exported", path)

    def _save_log_copy(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save log copy",
            initialfile=f"helios_log_{compact_stamp()}.txt",
            defaultextension=".txt",
            filetypes=[("Text", "*.txt")],
        )
        if not path:
            return
        Path(path).write_text("\n".join(self._log_lines), encoding="utf-8")

    def _clear_log_view(self) -> None:
        self._log_lines.clear()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _open_folder(self, path: Path) -> None:
        try:
            if IS_WINDOWS:
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                webbrowser.open(path.as_uri())
        except Exception as exc:
            messagebox.showerror("Open folder", str(exc))

    def _relaunch_admin(self) -> None:
        if is_admin():
            messagebox.showinfo("Administrator", "Helios is already running as administrator.")
            return
        ok, message = WindowsOps.relaunch_as_admin()
        if ok:
            self.shutdown()
        else:
            messagebox.showerror("Administrator relaunch", message)

    def _start_tray(self) -> None:
        if pystray is None or Image is None or ImageDraw is None:
            LOGGER.log("System tray support unavailable. Install pystray and pillow.", "WARN")
            return
        if self.tray_icon is not None:
            return
        try:
            image = Image.new("RGB", (64, 64), "#d9d9d9")
            draw = ImageDraw.Draw(image)
            draw.rectangle((6, 6, 58, 58), fill="#2c2c2c", outline="#ffffff", width=2)
            draw.line((18, 44, 30, 18, 36, 34, 47, 18), fill="#ffffff", width=5)
            menu = pystray.Menu(
                pystray.MenuItem("Open Helios", lambda _icon, _item: self.after(0, self.show_from_tray), default=True),
                pystray.MenuItem("Pause optimizer", lambda _icon, _item: self.after(0, self._toggle_pause), checked=lambda _item: self.optimizer.paused),
                pystray.MenuItem("Gaming profile", lambda _icon, _item: self.after(0, lambda: self._apply_profile("Gaming"))),
                pystray.MenuItem("Balanced profile", lambda _icon, _item: self.after(0, lambda: self._apply_profile("Balanced"))),
                pystray.MenuItem("Check for updates", lambda _icon, _item: self.after(0, lambda: self._check_for_updates(manual=True))),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", lambda _icon, _item: self.after(0, self.shutdown)),
            )
            self.tray_icon = pystray.Icon(APP_REGISTRY_NAME, image, APP_NAME, menu)
            threading.Thread(target=self.tray_icon.run, name="HeliosTray", daemon=True).start()
            LOGGER.log("System tray icon started.")
        except Exception as exc:
            LOGGER.log(f"Tray icon failed: {exc}", "WARN")

    def _toggle_pause(self) -> None:
        self.optimizer.paused = not self.optimizer.paused
        state = "paused" if self.optimizer.paused else "resumed"
        LOGGER.log(f"Automatic optimizer {state}.", "ACTION")

    def hide_to_tray(self) -> None:
        if self.tray_icon is None and pystray is None:
            self.iconify()
            return
        self.withdraw()

    def show_from_tray(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def on_close(self) -> None:
        if self.settings.minimize_to_tray:
            self.hide_to_tray()
        else:
            self.shutdown()

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        try:
            self.settings.last_window_geometry = self.geometry()
            self.settings.save()
        except Exception:
            pass
        LOGGER.log("Helios is shutting down.")
        self.stop_event.set()
        try:
            self.optimizer.shutdown()
        except Exception:
            LOGGER.log(traceback.format_exc(), "ERROR")
        try:
            if self.tray_icon is not None:
                self.tray_icon.stop()
        except Exception:
            pass
        try:
            self.telemetry.close(clean_shutdown=True)
        except Exception:
            LOGGER.log(traceback.format_exc(), "ERROR")
        try:
            self.crash_guard.clean_shutdown()
        except Exception:
            pass
        self.after(50, self.destroy)


def show_fatal_error(message: str) -> None:
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, message)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)


def main() -> None:
    if psutil is None:
        show_fatal_error("The psutil package is required.\n\nRun:\npython -m pip install psutil pystray pillow")
        return
    instance = SingleInstance("Local\\HeliosPerformanceHub")
    acquired = instance.acquire()
    if not acquired and "--admin-relaunch" in sys.argv:
        # The non-elevated copy needs a brief moment to close and release its mutex.
        for _ in range(20):
            time.sleep(0.2)
            instance.release()
            instance = SingleInstance("Local\\HeliosPerformanceHub")
            if instance.acquire():
                acquired = True
                break
    if not acquired:
        show_fatal_error("Helios Performance Control Hub is already running. Check the system tray.")
        return
    minimized = "--minimized" in sys.argv
    try:
        app = HeliosHub(minimized=minimized)
        app.mainloop()
    except Exception:
        error = traceback.format_exc()
        LOGGER.log(error, "FATAL")
        show_fatal_error(f"Helios encountered an unexpected error.\n\n{error[-1800:]}")
    finally:
        instance.release()


if __name__ == "__main__":
    main()
