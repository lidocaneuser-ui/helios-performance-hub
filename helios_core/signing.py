"""Ed25519 release signing helpers for Helios update packages."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def _crypto():
    try:
        from cryptography.hazmat.primitives import serialization  # type: ignore
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # type: ignore
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "The cryptography package is required for signed updates. "
            "Run: python -m pip install -r requirements.txt"
        ) from exc
    return serialization, Ed25519PrivateKey, Ed25519PublicKey


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def public_key_fingerprint(public_key_path: Path) -> str:
    data = Path(public_key_path).read_bytes()
    return hashlib.sha256(data).hexdigest()


def public_key_bytes_from_private(private_key_path: Path) -> bytes:
    """Derive the canonical PEM public key for an Ed25519 private key."""
    serialization, _, _ = _crypto()
    private_key_path = Path(private_key_path)
    private_key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def restore_public_key(private_key_path: Path, public_key_path: Path) -> Path:
    """Recreate a missing or empty public key from the existing private key."""
    public_key_path = Path(public_key_path)
    public_key_path.parent.mkdir(parents=True, exist_ok=True)
    public_key_path.write_bytes(public_key_bytes_from_private(Path(private_key_path)))
    return public_key_path


def keypair_matches(private_key_path: Path, public_key_path: Path) -> bool:
    """Return True only when the public key belongs to the supplied private key."""
    public_key_path = Path(public_key_path)
    if not public_key_path.is_file() or public_key_path.stat().st_size == 0:
        return False
    try:
        return public_key_path.read_bytes() == public_key_bytes_from_private(Path(private_key_path))
    except Exception:
        return False


def generate_keypair(private_key_path: Path, public_key_path: Path) -> Tuple[Path, Path]:
    serialization, Ed25519PrivateKey, _ = _crypto()
    private_key_path = Path(private_key_path)
    public_key_path = Path(public_key_path)
    private_key_path.parent.mkdir(parents=True, exist_ok=True)
    public_key_path.parent.mkdir(parents=True, exist_ok=True)
    if private_key_path.exists():
        raise FileExistsError(f"Private signing key already exists: {private_key_path}")
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_key_path.write_bytes(private_bytes)
    public_key_path.write_bytes(public_bytes)
    try:
        private_key_path.chmod(0o600)
    except Exception:
        pass
    return private_key_path, public_key_path


def sign_release(package_path: Path, private_key_path: Path, public_key_path: Optional[Path] = None) -> Dict[str, Any]:
    serialization, _, _ = _crypto()
    package_path = Path(package_path)
    private_key_path = Path(private_key_path)
    private_key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
    digest_hex = sha256_file(package_path)
    signature = private_key.sign(bytes.fromhex(digest_hex))
    payload: Dict[str, Any] = {
        "format": 1,
        "algorithm": "Ed25519-SHA256",
        "package": package_path.name,
        "sha256": digest_hex,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    if public_key_path and Path(public_key_path).is_file():
        payload["public_key_fingerprint"] = public_key_fingerprint(Path(public_key_path))
    return payload


def write_signature(package_path: Path, private_key_path: Path, signature_path: Path, public_key_path: Optional[Path] = None) -> Path:
    payload = sign_release(package_path, private_key_path, public_key_path)
    signature_path = Path(signature_path)
    signature_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return signature_path


def verify_release(package_path: Path, signature_text: str, public_key_path: Path) -> Tuple[bool, str]:
    serialization, _, _ = _crypto()
    try:
        payload = json.loads(signature_text)
        if not isinstance(payload, dict):
            return False, "Signature payload is not an object."
        if payload.get("algorithm") != "Ed25519-SHA256":
            return False, "Unsupported signature algorithm."
        digest_hex = sha256_file(Path(package_path))
        if str(payload.get("sha256", "")).lower() != digest_hex.lower():
            return False, "Signature digest does not match the package."
        if payload.get("package") and payload.get("package") != Path(package_path).name:
            return False, "Signature package name does not match."
        public_bytes = Path(public_key_path).read_bytes()
        expected_fingerprint = payload.get("public_key_fingerprint")
        if expected_fingerprint and expected_fingerprint != hashlib.sha256(public_bytes).hexdigest():
            return False, "Signature key fingerprint does not match the trusted key."
        public_key = serialization.load_pem_public_key(public_bytes)
        public_key.verify(base64.b64decode(str(payload.get("signature", ""))), bytes.fromhex(digest_hex))
        return True, "Ed25519 signature verified."
    except Exception as exc:
        return False, str(exc)
