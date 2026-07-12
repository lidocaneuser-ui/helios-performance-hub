# Helios 5 Update and Release System

## Components

- `helios_performance_hub.py`: running control hub and Updates tab.
- `helios_update.py`: GitHub release discovery, download, SHA-256 and Ed25519 verification, archive validation, and staging.
- `helios_update_worker.py`: out-of-process installation, backup, replacement, and rollback.
- `helios_launcher.py`: startup entry point, failed-update recovery, and source dependency synchronization.
- `helios_release.py`: package construction, manifest validation, signing, source snapshots, and GitHub publishing.

The updater protocol remains version 1 so every installed Helios 4.x source client can accept the 5.0.0 transition package. New release-format metadata is additive.

## GitHub release contract

A stable release uses a semantic tag such as `v5.0.0` and contains:

- `HeliosPerformanceHub-5.0.0.zip`
- `HeliosPerformanceHub-5.0.0.zip.sha256`
- `HeliosPerformanceHub-5.0.0.zip.sig` once release signing is configured

The ZIP contains `release.json` at its root. The checksum contains the package SHA-256 digest. The signature is a JSON detached signature over that digest using Ed25519.

## Trust migration

The 4.x updater validates the 5.0.0 package with SHA-256 because 4.x does not yet possess the 5.0 trust key. `Developer_Setup.cmd` generates the owner's private key and writes its public key into `release_public_key.pem` before 5.0.0 is packaged. Once 5.0 is installed, the updater detects that non-empty public key and rejects future releases that lack a valid matching signature.

The private key is stored at `%USERPROFILE%\.helios-release\ed25519-private.pem`. It must never be committed or published. Back it up securely.

## Publishing

The normal production workflow is:

1. Develop in `%USERPROFILE%\Documents\GitHub\helios-performance-hub`.
2. Update the application version and release documentation.
3. Run `Publish_Update.cmd`.
4. The script installs dependencies, compiles, runs tests, commits, pushes, builds, signs, and publishes.

Direct release builder usage:

```powershell
py -3 helios_release.py --mode source --publish lidocaneuser-ui/helios-performance-hub --signing-key "$env:USERPROFILE\.helios-release\ed25519-private.pem"
```

## Client validation

Before replacement, Helios requires GitHub HTTPS assets, enforces compressed and expanded size limits, verifies the SHA-256 digest, verifies the Ed25519 signature when a trust key is installed, rejects path traversal and symbolic links, validates the application ID/version/protocol/install mode, and confirms all required files are present.

## Installation and recovery

The update worker waits for Helios to exit, backs up replaceable program files, preserves the source virtual environment, copies the staged version, validates entry points, writes update-health metadata, and launches the new build. The launcher synchronizes changed requirements before importing the new app. A failed install restores the backup immediately; repeated failed post-update launches trigger automatic rollback.

## Channels

Stable ignores GitHub prereleases. Beta considers both stable and prerelease tags and selects the highest newer semantic version. Draft releases are always ignored.
