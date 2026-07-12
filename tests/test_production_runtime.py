from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from helios_core.production import CrashGuard, HealthAnalyzer, TelemetryDatabase, build_html_report
from helios_core.signing import generate_keypair, verify_release, write_signature


class ProductionRuntimeTests(unittest.TestCase):
    def test_crash_guard_safe_mode_and_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = CrashGuard(root, "5.0.0")
            self.assertFalse(first.begin())
            second = CrashGuard(root, "5.0.0")
            self.assertFalse(second.begin())
            third = CrashGuard(root, "5.0.0")
            self.assertTrue(third.begin())
            third.clean_shutdown()
            self.assertFalse((root / "runtime" / "running.json").exists())
            state = json.loads((root / "runtime" / "crash_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["unclean_count"], 0)

    def test_health_assessment_and_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "telemetry.db"
            database = TelemetryDatabase(path, version="5.0.0", sample_interval_seconds=1)
            database.start_session(safe_mode=False)
            snapshot = SimpleNamespace(
                timestamp=time.time(), cpu_percent=98.0, memory_percent=91.0,
                disk_percent=97.0, gpu_percent=99.0, gpu_temperature_c=88.0,
                swap_percent=92.0, network_send_rate=100.0, network_recv_rate=200.0,
                foreground_name="game.exe",
            )
            assessment = HealthAnalyzer().assess(snapshot)
            self.assertLess(assessment.score, 55)
            database.record_snapshot(snapshot, assessment)
            database.record_action("test_action", {"target": "game.exe"})
            time.sleep(0.8)
            samples = database.recent_samples(minutes=5)
            actions = database.recent_actions(limit=10)
            self.assertEqual(len(samples), 1)
            self.assertEqual(actions[0]["event_type"], "test_action")
            ok, detail = database.integrity_check()
            self.assertTrue(ok, detail)
            database.close(clean_shutdown=True)

    def test_html_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "report.html"
            build_html_report(
                destination,
                app_version="5.0.0",
                machine_name="TEST-PC",
                samples=[{"cpu_percent": 10, "memory_percent": 20, "disk_percent": 5, "gpu_percent": 30}],
                actions=[{"timestamp": time.time(), "event_type": "profile", "details": {"name": "Gaming"}}],
                summary={"samples": 1, "avg_cpu": 10.0},
                diagnostics=[{"name": "Database", "status": "PASS", "detail": "ok"}],
            )
            text = destination.read_text(encoding="utf-8")
            self.assertIn("Helios Performance Control Hub 5.0.0", text)
            self.assertIn("TEST-PC", text)


class SigningTests(unittest.TestCase):
    def test_ed25519_release_signature(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_key = root / "private.pem"
            public_key = root / "public.pem"
            package = root / "HeliosPerformanceHub-5.0.0.zip"
            signature = root / "HeliosPerformanceHub-5.0.0.zip.sig"
            package.write_bytes(b"verified package")
            generate_keypair(private_key, public_key)
            write_signature(package, private_key, signature, public_key)
            ok, detail = verify_release(package, signature.read_text(encoding="utf-8"), public_key)
            self.assertTrue(ok, detail)
            package.write_bytes(b"tampered package")
            ok, _ = verify_release(package, signature.read_text(encoding="utf-8"), public_key)
            self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
