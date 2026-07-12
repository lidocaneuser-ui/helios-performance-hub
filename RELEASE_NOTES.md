# Helios Performance Control Hub 5.0.0

Helios 5 is the production-foundation release. It converts the control hub from a capable local optimizer into a maintainable, auditable, recoverable desktop product.

## New production systems

- Persistent SQLite performance history with CPU, memory, disk, GPU, temperature, network, foreground workload, and health-score samples.
- Health & History dashboard with configurable time windows, trend graph, session statistics, and optimizer audit trail.
- Crash-loop detection and automatic safe mode after repeated unclean exits.
- One-click self-diagnostics and exportable HTML performance reports.
- Automatic dependency migration after source updates, so future versions can safely add or update libraries.
- Ed25519 release signing. After 5.0 is installed with its trust key, unsigned or tampered future updates are rejected.
- Structured repository layout, continuous-integration checks, production tests, security policy, changelog, contribution standards, and uninstall support.

## Reliability and safety

- Update protocol remains compatible with Helios 4.x for a seamless 5.0 upgrade.
- SHA-256 verification, protected ZIP extraction, staged installation, program backup, rollback, and failed-launch recovery remain enabled.
- Telemetry stays on the local PC and uses bounded retention.
- Safe mode pauses automatic tuning without blocking monitoring, diagnostics, manual controls, or updates.
- Existing safety boundaries remain: no Real-Time priority, no undocumented GPU timeout edits, no Windows Security disabling, no mass process termination, and no automatic overclocking.
