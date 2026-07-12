"""Production runtime services for Helios Performance Control Hub.

This module deliberately uses the Python standard library so the core monitor can
remain operational even when an optional integration is unavailable. It provides:

* crash-loop detection and safe-mode startup
* asynchronous SQLite telemetry and action auditing
* bounded data retention and integrity checks
* health scoring with deduplicated alerts
* self-diagnostics and portable HTML reports
"""

from __future__ import annotations

import datetime as dt
from contextlib import closing
import html
import json
import os
import platform
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

SCHEMA_VERSION = 1
DEFAULT_RETENTION_DAYS = 30
MAX_QUEUE_ITEMS = 10_000


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(dict(payload), handle, indent=2, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


@dataclass(frozen=True)
class HealthAssessment:
    score: int
    grade: str
    summary: str
    alerts: Tuple[Dict[str, Any], ...]


class HealthAnalyzer:
    """Computes a conservative system-readiness score from a telemetry snapshot."""

    def assess(self, snapshot: Any) -> HealthAssessment:
        score = 100
        alerts: List[Dict[str, Any]] = []

        def alert(severity: str, code: str, message: str, value: Optional[float] = None) -> None:
            alerts.append(
                {
                    "severity": severity,
                    "code": code,
                    "message": message,
                    "metric_value": value,
                }
            )

        cpu = float(getattr(snapshot, "cpu_percent", 0.0) or 0.0)
        memory = float(getattr(snapshot, "memory_percent", 0.0) or 0.0)
        disk = float(getattr(snapshot, "disk_percent", 0.0) or 0.0)
        gpu = getattr(snapshot, "gpu_percent", None)
        gpu_temp = getattr(snapshot, "gpu_temperature_c", None)
        swap = float(getattr(snapshot, "swap_percent", 0.0) or 0.0)

        if cpu >= 97:
            score -= 25
            alert("critical", "cpu_saturation", "CPU utilization is saturated.", cpu)
        elif cpu >= 90:
            score -= 16
            alert("warning", "cpu_pressure", "CPU utilization is under sustained pressure.", cpu)
        elif cpu >= 75:
            score -= 6

        if memory >= 95:
            score -= 28
            alert("critical", "memory_exhaustion", "Physical memory is nearly exhausted.", memory)
        elif memory >= 88:
            score -= 17
            alert("warning", "memory_pressure", "Physical memory pressure is high.", memory)
        elif memory >= 78:
            score -= 7

        if swap >= 90:
            score -= 12
            alert("warning", "pagefile_pressure", "Virtual memory utilization is high.", swap)

        if disk >= 98:
            score -= 18
            alert("critical", "disk_saturation", "Disk active time is saturated.", disk)
        elif disk >= 92:
            score -= 10
            alert("warning", "disk_pressure", "Disk active time is high.", disk)

        if gpu_temp is not None:
            temperature = float(gpu_temp)
            if temperature >= 90:
                score -= 25
                alert("critical", "gpu_temperature", "GPU temperature is critically high.", temperature)
            elif temperature >= 84:
                score -= 14
                alert("warning", "gpu_temperature", "GPU temperature is high.", temperature)
            elif temperature >= 79:
                score -= 5

        if gpu is not None and float(gpu) >= 99 and cpu >= 90:
            score -= 5
            alert("info", "combined_saturation", "CPU and GPU are both near full utilization.")

        score = max(0, min(100, int(round(score))))
        if score >= 90:
            grade = "READY"
        elif score >= 75:
            grade = "HEALTHY"
        elif score >= 55:
            grade = "WATCH"
        else:
            grade = "CONSTRAINED"

        if alerts:
            highest = next((a for a in alerts if a["severity"] == "critical"), alerts[0])
            summary = str(highest["message"])
        else:
            summary = "System resources are within the configured operating envelope."
        return HealthAssessment(score=score, grade=grade, summary=summary, alerts=tuple(alerts))


class CrashGuard:
    """Detects unclean exits and enters safe mode after a crash loop."""

    def __init__(self, data_root: Path, version: str, threshold: int = 2) -> None:
        self.data_root = Path(data_root)
        self.version = version
        self.threshold = max(2, int(threshold))
        self.state_path = self.data_root / "runtime" / "crash_state.json"
        self.marker_path = self.data_root / "runtime" / "running.json"
        self.safe_mode = False
        self.unclean_count = 0

    def begin(self) -> bool:
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        state = _read_json(self.state_path)
        previous_marker = _read_json(self.marker_path)
        now = time.time()
        last_unclean = float(state.get("last_unclean_epoch", 0.0) or 0.0)
        count = int(state.get("unclean_count", 0) or 0)

        if previous_marker:
            marker_age = now - float(previous_marker.get("started_epoch", now) or now)
            if marker_age <= 24 * 60 * 60:
                count += 1
            else:
                count = 1
            last_unclean = now
        elif last_unclean and now - last_unclean > 48 * 60 * 60:
            count = 0

        self.unclean_count = count
        self.safe_mode = count >= self.threshold
        _atomic_json(
            self.state_path,
            {
                "unclean_count": count,
                "last_unclean_epoch": last_unclean,
                "safe_mode": self.safe_mode,
                "version": self.version,
                "updated_at": _utc_now(),
            },
        )
        _atomic_json(
            self.marker_path,
            {
                "pid": os.getpid(),
                "version": self.version,
                "started_epoch": now,
                "started_at": _utc_now(),
                "safe_mode": self.safe_mode,
            },
        )
        return self.safe_mode

    def clean_shutdown(self) -> None:
        self.marker_path.unlink(missing_ok=True)
        _atomic_json(
            self.state_path,
            {
                "unclean_count": 0,
                "last_unclean_epoch": 0.0,
                "safe_mode": False,
                "version": self.version,
                "updated_at": _utc_now(),
            },
        )
        self.unclean_count = 0
        self.safe_mode = False

    def reset(self) -> None:
        self.clean_shutdown()


class TelemetryDatabase:
    """Asynchronous, bounded SQLite store for samples, sessions, actions, and alerts."""

    def __init__(
        self,
        database_path: Path,
        *,
        version: str,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        sample_interval_seconds: int = 5,
    ) -> None:
        self.path = Path(database_path)
        self.version = version
        self.retention_days = max(1, min(365, int(retention_days)))
        self.sample_interval_seconds = max(1, min(60, int(sample_interval_seconds)))
        self.session_id = uuid.uuid4().hex
        self.started_epoch = time.time()
        self._last_sample_epoch = 0.0
        self._last_alert_epoch: Dict[str, float] = {}
        self._queue: "queue.Queue[Tuple[str, Dict[str, Any]]]" = queue.Queue(MAX_QUEUE_ITEMS)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._writer_loop, name="HeliosTelemetryDB", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=8)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                version TEXT NOT NULL,
                started_epoch REAL NOT NULL,
                started_at TEXT NOT NULL,
                ended_epoch REAL,
                ended_at TEXT,
                clean_shutdown INTEGER NOT NULL DEFAULT 0,
                safe_mode INTEGER NOT NULL DEFAULT 0,
                samples INTEGER NOT NULL DEFAULT 0,
                avg_cpu REAL,
                peak_cpu REAL,
                avg_memory REAL,
                peak_memory REAL,
                avg_disk REAL,
                peak_disk REAL,
                avg_gpu REAL,
                peak_gpu REAL,
                min_health INTEGER
            );
            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                cpu_percent REAL NOT NULL,
                memory_percent REAL NOT NULL,
                disk_percent REAL NOT NULL,
                gpu_percent REAL,
                gpu_temperature_c REAL,
                swap_percent REAL NOT NULL,
                network_send_rate REAL NOT NULL,
                network_recv_rate REAL NOT NULL,
                foreground_name TEXT NOT NULL,
                health_score INTEGER NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_samples_timestamp ON samples(timestamp);
            CREATE INDEX IF NOT EXISTS idx_samples_session ON samples(session_id, timestamp);
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                session_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                level TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_actions_timestamp ON actions(timestamp);
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                session_id TEXT NOT NULL,
                severity TEXT NOT NULL,
                code TEXT NOT NULL,
                message TEXT NOT NULL,
                metric_value REAL
            );
            CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts(timestamp);
            """
        )
        connection.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )

    def start_session(self, safe_mode: bool = False) -> None:
        self._enqueue(
            "session_start",
            {
                "session_id": self.session_id,
                "version": self.version,
                "started_epoch": self.started_epoch,
                "started_at": _utc_now(),
                "safe_mode": int(bool(safe_mode)),
            },
        )

    def record_snapshot(self, snapshot: Any, assessment: HealthAssessment) -> None:
        timestamp = float(getattr(snapshot, "timestamp", time.time()) or time.time())
        if timestamp - self._last_sample_epoch < self.sample_interval_seconds:
            return
        self._last_sample_epoch = timestamp
        payload = {
            "session_id": self.session_id,
            "timestamp": timestamp,
            "cpu_percent": float(getattr(snapshot, "cpu_percent", 0.0) or 0.0),
            "memory_percent": float(getattr(snapshot, "memory_percent", 0.0) or 0.0),
            "disk_percent": float(getattr(snapshot, "disk_percent", 0.0) or 0.0),
            "gpu_percent": getattr(snapshot, "gpu_percent", None),
            "gpu_temperature_c": getattr(snapshot, "gpu_temperature_c", None),
            "swap_percent": float(getattr(snapshot, "swap_percent", 0.0) or 0.0),
            "network_send_rate": float(getattr(snapshot, "network_send_rate", 0.0) or 0.0),
            "network_recv_rate": float(getattr(snapshot, "network_recv_rate", 0.0) or 0.0),
            "foreground_name": str(getattr(snapshot, "foreground_name", "") or ""),
            "health_score": int(assessment.score),
        }
        self._enqueue("sample", payload)
        now = time.time()
        for alert in assessment.alerts:
            code = str(alert.get("code", "unknown"))
            if now - self._last_alert_epoch.get(code, 0.0) < 60:
                continue
            self._last_alert_epoch[code] = now
            self._enqueue(
                "alert",
                {
                    "timestamp": timestamp,
                    "session_id": self.session_id,
                    "severity": str(alert.get("severity", "info")),
                    "code": code,
                    "message": str(alert.get("message", "")),
                    "metric_value": alert.get("metric_value"),
                },
            )

    def record_action(self, event_type: str, details: Mapping[str, Any], level: str = "ACTION") -> None:
        self._enqueue(
            "action",
            {
                "timestamp": time.time(),
                "session_id": self.session_id,
                "event_type": str(event_type),
                "level": str(level),
                "details_json": json.dumps(dict(details), default=str, sort_keys=True),
            },
        )

    def _enqueue(self, kind: str, payload: Dict[str, Any]) -> None:
        try:
            self._queue.put_nowait((kind, payload))
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait((kind, payload))
            except Exception:
                pass

    def _writer_loop(self) -> None:
        connection: Optional[sqlite3.Connection] = None
        try:
            connection = self._connect()
            self._create_schema(connection)
            self._ready.set()
            while not self._stop.is_set() or not self._queue.empty():
                batch: List[Tuple[str, Dict[str, Any]]] = []
                try:
                    batch.append(self._queue.get(timeout=0.4))
                except queue.Empty:
                    continue
                while len(batch) < 200:
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
                connection.execute("BEGIN")
                try:
                    for kind, payload in batch:
                        self._apply(connection, kind, payload)
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
            self._prune(connection)
        finally:
            self._ready.set()
            if connection is not None:
                connection.close()

    @staticmethod
    def _apply(connection: sqlite3.Connection, kind: str, payload: Dict[str, Any]) -> None:
        if kind == "session_start":
            connection.execute(
                "INSERT OR REPLACE INTO sessions(session_id, version, started_epoch, started_at, safe_mode) "
                "VALUES(:session_id, :version, :started_epoch, :started_at, :safe_mode)",
                payload,
            )
        elif kind == "sample":
            connection.execute(
                """
                INSERT INTO samples(
                    session_id, timestamp, cpu_percent, memory_percent, disk_percent,
                    gpu_percent, gpu_temperature_c, swap_percent, network_send_rate,
                    network_recv_rate, foreground_name, health_score
                ) VALUES(
                    :session_id, :timestamp, :cpu_percent, :memory_percent, :disk_percent,
                    :gpu_percent, :gpu_temperature_c, :swap_percent, :network_send_rate,
                    :network_recv_rate, :foreground_name, :health_score
                )
                """,
                payload,
            )
        elif kind == "action":
            connection.execute(
                "INSERT INTO actions(timestamp, session_id, event_type, level, details_json) "
                "VALUES(:timestamp, :session_id, :event_type, :level, :details_json)",
                payload,
            )
        elif kind == "alert":
            connection.execute(
                "INSERT INTO alerts(timestamp, session_id, severity, code, message, metric_value) "
                "VALUES(:timestamp, :session_id, :severity, :code, :message, :metric_value)",
                payload,
            )
        elif kind == "session_end":
            connection.execute(
                """
                UPDATE sessions SET
                    ended_epoch=:ended_epoch,
                    ended_at=:ended_at,
                    clean_shutdown=:clean_shutdown,
                    samples=(SELECT COUNT(*) FROM samples WHERE session_id=:session_id),
                    avg_cpu=(SELECT AVG(cpu_percent) FROM samples WHERE session_id=:session_id),
                    peak_cpu=(SELECT MAX(cpu_percent) FROM samples WHERE session_id=:session_id),
                    avg_memory=(SELECT AVG(memory_percent) FROM samples WHERE session_id=:session_id),
                    peak_memory=(SELECT MAX(memory_percent) FROM samples WHERE session_id=:session_id),
                    avg_disk=(SELECT AVG(disk_percent) FROM samples WHERE session_id=:session_id),
                    peak_disk=(SELECT MAX(disk_percent) FROM samples WHERE session_id=:session_id),
                    avg_gpu=(SELECT AVG(gpu_percent) FROM samples WHERE session_id=:session_id),
                    peak_gpu=(SELECT MAX(gpu_percent) FROM samples WHERE session_id=:session_id),
                    min_health=(SELECT MIN(health_score) FROM samples WHERE session_id=:session_id)
                WHERE session_id=:session_id
                """,
                payload,
            )

    def _prune(self, connection: sqlite3.Connection) -> None:
        cutoff = time.time() - self.retention_days * 86400
        connection.execute("DELETE FROM samples WHERE timestamp < ?", (cutoff,))
        connection.execute("DELETE FROM actions WHERE timestamp < ?", (cutoff,))
        connection.execute("DELETE FROM alerts WHERE timestamp < ?", (cutoff,))
        connection.execute("DELETE FROM sessions WHERE started_epoch < ? AND ended_epoch IS NOT NULL", (cutoff,))
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self, clean_shutdown: bool = True, timeout: float = 5.0) -> None:
        self._enqueue(
            "session_end",
            {
                "session_id": self.session_id,
                "ended_epoch": time.time(),
                "ended_at": _utc_now(),
                "clean_shutdown": int(bool(clean_shutdown)),
            },
        )
        self._stop.set()
        self._thread.join(timeout=max(0.5, timeout))

    def recent_samples(self, minutes: int = 30, limit: int = 720) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        cutoff = time.time() - max(1, minutes) * 60
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT timestamp, cpu_percent, memory_percent, disk_percent, gpu_percent, "
                    "gpu_temperature_c, health_score FROM samples WHERE timestamp >= ? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (cutoff, max(10, min(5000, int(limit)))),
                ).fetchall()
            return [dict(row) for row in reversed(rows)]
        except Exception:
            return []

    def recent_actions(self, limit: int = 100) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT timestamp, event_type, level, details_json FROM actions "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (max(1, min(1000, int(limit))),),
                ).fetchall()
            result: List[Dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                try:
                    item["details"] = json.loads(item.pop("details_json"))
                except Exception:
                    item["details"] = {"raw": item.pop("details_json", "")}
                result.append(item)
            return result
        except Exception:
            return []

    def session_summary(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS samples, AVG(cpu_percent) AS avg_cpu, MAX(cpu_percent) AS peak_cpu,
                           AVG(memory_percent) AS avg_memory, MAX(memory_percent) AS peak_memory,
                           AVG(disk_percent) AS avg_disk, MAX(disk_percent) AS peak_disk,
                           AVG(gpu_percent) AS avg_gpu, MAX(gpu_percent) AS peak_gpu,
                           MIN(health_score) AS min_health, AVG(health_score) AS avg_health
                    FROM samples WHERE session_id=?
                    """,
                    (self.session_id,),
                ).fetchone()
            return dict(row) if row else {}
        except Exception:
            return {}

    def integrity_check(self) -> Tuple[bool, str]:
        if not self.path.exists():
            return True, "Database will be created on first sample."
        try:
            with closing(self._connect()) as connection:
                result = connection.execute("PRAGMA quick_check").fetchone()
            text = str(result[0]) if result else "unknown"
            return text.lower() == "ok", text
        except Exception as exc:
            return False, str(exc)

    def clear_history(self) -> None:
        if not self.path.exists():
            return
        with closing(self._connect()) as connection:
            connection.execute("DELETE FROM samples WHERE session_id <> ?", (self.session_id,))
            connection.execute("DELETE FROM actions WHERE session_id <> ?", (self.session_id,))
            connection.execute("DELETE FROM alerts WHERE session_id <> ?", (self.session_id,))
            connection.execute("DELETE FROM sessions WHERE session_id <> ?", (self.session_id,))
            connection.execute("VACUUM")


def run_self_diagnostics(
    *,
    app_root: Path,
    data_root: Path,
    settings_path: Path,
    database: Optional[TelemetryDatabase] = None,
) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        results.append({"name": name, "status": "PASS" if ok else "FAIL", "detail": detail})

    add("Python runtime", sys.version_info >= (3, 11), platform.python_version())
    add("Windows platform", os.name == "nt", platform.platform())
    try:
        data_root.mkdir(parents=True, exist_ok=True)
        probe = data_root / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        add("Data directory", True, str(data_root))
    except Exception as exc:
        add("Data directory", False, str(exc))

    required = [
        "helios_performance_hub.py",
        "helios_update.py",
        "helios_update_worker.py",
        "helios_launcher.py",
        "requirements.txt",
        "release.json",
    ]
    missing = [name for name in required if not (app_root / name).is_file()]
    add("Program files", not missing, "Complete" if not missing else "Missing: " + ", ".join(missing))

    try:
        value = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
        add("Settings file", isinstance(value, dict), str(settings_path))
    except Exception as exc:
        add("Settings file", False, str(exc))

    if database is not None:
        ok, detail = database.integrity_check()
        add("Telemetry database", ok, detail)

    for executable, label in (("powercfg", "Windows power service"), ("nvidia-smi", "NVIDIA telemetry")):
        path = shutil.which(executable)
        if path:
            try:
                completed = subprocess.run(
                    [path, "/GETACTIVESCHEME"] if executable == "powercfg" else [path, "--query-gpu=name", "--format=csv,noheader"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
                )
                add(label, completed.returncode == 0, (completed.stdout or completed.stderr).strip()[:240])
            except Exception as exc:
                add(label, False, str(exc))
        else:
            add(label, executable != "powercfg", "Not found on PATH")
    return results


def _svg_polyline(samples: Sequence[Mapping[str, Any]], key: str, width: int, height: int) -> str:
    values: List[float] = []
    for row in samples:
        value = row.get(key)
        values.append(float(value) if value is not None else 0.0)
    if not values:
        return ""
    denominator = max(1, len(values) - 1)
    points = []
    for index, value in enumerate(values):
        x = index / denominator * width
        y = height - max(0.0, min(100.0, value)) / 100.0 * height
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def build_html_report(
    destination: Path,
    *,
    app_version: str,
    machine_name: str,
    samples: Sequence[Mapping[str, Any]],
    actions: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
    diagnostics: Sequence[Mapping[str, str]],
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    width, height = 900, 220
    series = [
        ("CPU", "cpu_percent", "#202020"),
        ("Memory", "memory_percent", "#6b6b6b"),
        ("Disk", "disk_percent", "#9a5b00"),
        ("GPU", "gpu_percent", "#1f6b35"),
    ]
    polylines = "\n".join(
        f'<polyline points="{_svg_polyline(samples, key, width, height)}" fill="none" stroke="{color}" stroke-width="2" />'
        for _, key, color in series
    )
    legend = " ".join(f'<span style="color:{color}">■</span> {html.escape(label)}' for label, _, color in series)

    diagnostic_rows = "".join(
        f"<tr><td>{html.escape(str(row.get('name', '')))}</td>"
        f"<td>{html.escape(str(row.get('status', '')))}</td>"
        f"<td>{html.escape(str(row.get('detail', '')))}</td></tr>"
        for row in diagnostics
    )
    action_rows = "".join(
        f"<tr><td>{dt.datetime.fromtimestamp(float(row.get('timestamp', 0))).strftime('%H:%M:%S')}</td>"
        f"<td>{html.escape(str(row.get('event_type', '')))}</td>"
        f"<td><code>{html.escape(json.dumps(row.get('details', {}), default=str)[:500])}</code></td></tr>"
        for row in actions[:50]
    )
    summary_cards = "".join(
        f"<div class='card'><strong>{html.escape(str(key).replace('_', ' ').title())}</strong><br>"
        f"{html.escape(f'{value:.1f}' if isinstance(value, float) else str(value))}</div>"
        for key, value in summary.items()
        if value is not None
    )

    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Helios Performance Report</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;background:#dadada;color:#171717;margin:0}}
header{{background:#2c2c2c;color:white;padding:24px 34px}}
main{{max-width:1050px;margin:24px auto;padding:0 18px}}
section{{background:#eeeeee;border:1px solid #8a8a8a;margin:14px 0;padding:18px}}
.cards{{display:flex;flex-wrap:wrap;gap:10px}}.card{{background:#f8f8f8;border:1px solid #aaa;padding:12px;min-width:150px}}
table{{border-collapse:collapse;width:100%;background:#f8f8f8}}th,td{{border:1px solid #aaa;padding:8px;text-align:left;vertical-align:top}}
svg{{background:#f8f8f8;border:1px solid #aaa;width:100%;height:auto}}code{{white-space:pre-wrap;word-break:break-word}}
</style></head><body>
<header><h1>Helios Performance Control Hub {html.escape(app_version)}</h1>
<div>{html.escape(machine_name)} · generated {html.escape(generated)}</div></header>
<main>
<section><h2>Session summary</h2><div class="cards">{summary_cards}</div></section>
<section><h2>Performance history</h2><p>{legend}</p>
<svg viewBox="0 0 {width} {height}" role="img" aria-label="System utilization history">
<line x1="0" y1="55" x2="900" y2="55" stroke="#ddd"/><line x1="0" y1="110" x2="900" y2="110" stroke="#ddd"/><line x1="0" y1="165" x2="900" y2="165" stroke="#ddd"/>{polylines}</svg></section>
<section><h2>Self diagnostics</h2><table><thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead><tbody>{diagnostic_rows}</tbody></table></section>
<section><h2>Recent optimizer actions</h2><table><thead><tr><th>Time</th><th>Event</th><th>Details</th></tr></thead><tbody>{action_rows}</tbody></table></section>
</main></body></html>"""
    destination.write_text(document, encoding="utf-8")
    return destination
