# Changelog

## 5.0.0 — Production Foundation

- Added local SQLite telemetry, session summaries, action audit trail, bounded retention, integrity checks, and HTML reports.
- Added crash-loop detection and safe-mode startup after repeated unclean exits.
- Added Health & History dashboard with live readiness score and CPU, memory, disk, and GPU trend graph.
- Added self-diagnostics for runtime, program files, settings, data access, database integrity, Windows power controls, and NVIDIA telemetry.
- Added automatic dependency synchronization after source-mode updates.
- Added Ed25519-signed releases with a locally protected private key and bundled public trust key.
- Added production repository bootstrap, Git migration, one-command publishing, CI, linting, tests, security policy, and uninstall tooling.
- Preserved compatibility with installed 4.x updaters and existing rollback backups.

## 4.0.1

- Verified the GitHub update channel.
- Improved update reliability.
- Added minor stability fixes.
