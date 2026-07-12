# Helios Security Policy

## Update trust model

Helios 5.0 keeps SHA-256 verification and adds Ed25519 release signatures. The first 5.0 upgrade remains compatible with the 4.x updater. During production repository setup, a private signing key is created under `%USERPROFILE%\.helios-release`; only its public key is committed and distributed with Helios. Once a client has a non-empty `release_public_key.pem`, unsigned updates are rejected.

The private key must never be uploaded, committed, emailed, or placed inside a release ZIP. Losing it means a new trust migration release is required.

## Reporting a vulnerability

Do not publish exploit details in a public issue. Contact the repository owner privately with the affected version, reproduction steps, impact, and any proposed mitigation.

## Safety boundaries

Helios refuses update packages with path traversal, symbolic links, excessive file counts, excessive expanded size, wrong application identifiers, incompatible install modes, invalid hashes, or missing/invalid signatures when signature enforcement is active.
