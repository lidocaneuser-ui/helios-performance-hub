# Helios 5 Production Architecture

Helios 5 retains the dependable single-process desktop control surface while moving production-critical services into `helios_core`.

## Runtime layers

- `helios_performance_hub.py`: Tkinter UI, Windows controls, process inspection, profiles, and automatic optimizer.
- `helios_core/production.py`: crash-loop guard, safe mode, health analysis, SQLite telemetry, session summaries, diagnostics, retention, and HTML reporting.
- `helios_update.py`: GitHub release discovery, protected download/staging, SHA-256 and Ed25519 verification.
- `helios_update_worker.py`: out-of-process atomic replacement, backup, rollback, and recovery.
- `helios_launcher.py`: failed-update guard and automatic dependency synchronization after source updates.
- `helios_release.py`: deterministic package construction, manifest validation, signing, repository snapshots, and GitHub publishing.

## Data boundaries

Replaceable program files live under `%LOCALAPPDATA%\Programs\HeliosPerformanceHub`. Persistent settings, telemetry, logs, update staging, backups, and reports live under `%LOCALAPPDATA%\HeliosPerformanceHub`.

Telemetry is local-only. It is not uploaded by Helios. The default retention window is 30 days and can be changed in settings. SQLite uses WAL mode, batched asynchronous writes, bounded queues, and integrity checks.

## Failure handling

An active-run marker is written at startup and removed during a clean shutdown. Two recent unclean starts trigger safe mode, which pauses automatic optimization while leaving monitoring, diagnostics, updates, and manual controls available. Update failures restore the previous program backup; repeated failed post-update launches also trigger rollback.
