# Helios Performance Control Hub 5.0

Helios is an always-on Windows 11 performance, stability, and resource-management control hub. Version 5.0 is the production-foundation release: it adds local telemetry history, crash-safe startup, health analysis, signed updates, automated dependency migration, a real Git development workflow, and continuous validation.

## One-time user installation

1. Extract the release folder.
2. Run `Install_and_Run.cmd`.
3. Helios installs to `%LOCALAPPDATA%\Programs\HeliosPerformanceHub`.
4. Persistent settings, telemetry, logs, reports, update staging, and rollback backups remain under `%LOCALAPPDATA%\HeliosPerformanceHub`.
5. Start Menu and desktop shortcuts are created, and Helios starts minimized when you sign in unless disabled during installation.

## Production capabilities

- Continuous CPU, memory, disk, network, battery, foreground-process, and NVIDIA telemetry.
- Automatic game/workload detection with reversible CPU and I/O priority management.
- Gaming, Balanced, Creator, Quiet, and Maximum Performance profiles.
- Startup-entry review, page-file repair, shader-cache cleanup, event review, and diagnostics.
- Health & History dashboard with readiness score, trend graph, session summary, and optimizer audit trail.
- Local SQLite telemetry with asynchronous writes, WAL mode, integrity checks, and bounded retention.
- Crash-loop detection and safe mode after repeated unclean exits.
- Exportable HTML performance reports.
- Stable and Beta update channels, background checks, staged replacement, backup, rollback, and failed-start recovery.
- SHA-256 package validation plus Ed25519 signatures after the 5.0 trust key is installed.
- Automatic virtual-environment dependency synchronization after source-mode updates.

## Create the permanent development repository and publish 5.0

Run `Developer_Setup.cmd` from the extracted 5.0 source folder. It will:

- install Git for Windows through WinGet when necessary;
- clone `lidocaneuser-ui/helios-performance-hub` into `%USERPROFILE%\Documents\GitHub\helios-performance-hub`;
- copy the production source into that permanent development folder;
- configure Git author information using the authenticated GitHub account;
- generate a private Ed25519 release key under `%USERPROFILE%\.helios-release` and commit only its public key;
- install development dependencies;
- compile and test the project;
- commit and push the complete source tree;
- build, sign, and publish the `v5.0.0` GitHub Release.

After that, all future development should happen in the permanent Git folder. Update `APP_VERSION`, `pyproject.toml`, release notes, and changelog, then run `Publish_Update.cmd`.

## Build standalone executables

Run `Build_EXE.cmd`. It validates the tests first, then builds:

- `dist\HeliosPerformanceHub.exe`
- `dist\HeliosLauncher.exe`
- `dist\HeliosUpdater.exe`
- a Hybrid release package under `release_artifacts\<version>`

For broad public distribution, Authenticode-sign the executables in addition to Helios package signing.

## Safety boundaries

Helios does not disable Windows Security, use Real-Time process priority, mass-terminate processes, apply undocumented GPU timeout edits, overclock hardware, or install registry-tweak packs. Automatic changes are logged and designed to be restored.
