# Contributing to Helios

Helios changes must preserve the project's safety boundaries: no Real-Time process priority, no undocumented GPU timeout edits, no security-service disabling, no automatic mass process termination, and no irreversible system changes without an explicit backup and restore path.

## Development checks

```powershell
py -3 -m pip install -r requirements-dev.txt
py -3 -m unittest discover -s tests -v
py -3 -m compileall -q helios_performance_hub.py helios_update.py helios_update_worker.py helios_launcher.py helios_release.py helios_core
```

Every release must update `APP_VERSION`, `pyproject.toml`, `release.json`, `CHANGELOG.md`, and `RELEASE_NOTES.md`. Production releases must be signed with the private Ed25519 release key. Never commit that private key.
