HELIOS 5.0 RELEASE-SIGNING FIX

This patch fixes the first-run failure:
  release_public_key.pem must contain the matching public key before signing.

What it does:
- Reconstructs an empty/missing public key from the existing private Ed25519 key.
- Verifies the private and public keys match before publishing.
- Refuses to publish with a mismatched key pair.
- Makes migration key verification idempotent.

Install:
1. Extract this ZIP directly into the Helios_Performance_Hub_v5 folder that produced the error.
2. Choose "Replace the files in the destination."
3. Run Repair_Release_Signing.cmd once.
4. Rerun the production migration/publish command.

Never upload or share:
  %USERPROFILE%\.helios-release\ed25519-private.pem
