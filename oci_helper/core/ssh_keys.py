"""OpenSSH public-key validation, fingerprints, and key-pair generation."""

from __future__ import annotations

import base64
import hashlib
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_ssh_public_key,
)


def normalize_ssh_public_key(value: str, *, require_rsa: bool = False) -> str:
    """Validate and canonicalize one OpenSSH public key while preserving its comment."""
    normalized = value.strip()
    if not normalized or "\n" in normalized or "\r" in normalized:
        raise ValueError("must contain exactly one OpenSSH public key")
    if "\x00" in normalized:
        raise ValueError("must not contain NUL")

    parts = normalized.split(maxsplit=2)
    if len(parts) < 2:
        raise ValueError("must use OpenSSH public key format")
    key_type, encoded_key = parts[:2]
    try:
        public_key = load_ssh_public_key(f"{key_type} {encoded_key}".encode("ascii"))
        canonical_key = public_key.public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode(
            "ascii"
        )
    except (TypeError, UnicodeEncodeError, UnsupportedAlgorithm, ValueError) as exc:
        raise ValueError("contains invalid OpenSSH public key data") from exc

    if require_rsa and not canonical_key.startswith("ssh-rsa "):
        raise ValueError("OCI console connections require an RSA OpenSSH public key")

    if len(parts) == 2:
        return canonical_key
    comment = parts[2].strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in comment):
        raise ValueError("public key comment must not contain control characters")
    return f"{canonical_key} {comment}" if comment else canonical_key


def normalize_ssh_public_keys(value: str) -> str:
    """Validate and canonicalize one or more newline-separated OpenSSH public keys."""
    if "\x00" in value:
        raise ValueError("must not contain NUL")
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        raise ValueError("must contain at least one OpenSSH public key")
    return "\n".join(normalize_ssh_public_key(line) for line in lines)


def ssh_public_key_fingerprint(public_key: str) -> str:
    """Return the standard SHA256 fingerprint for one OpenSSH public key."""
    normalized = normalize_ssh_public_key(public_key)
    encoded_key = normalized.split(maxsplit=2)[1]
    key_blob = base64.b64decode(encoded_key, validate=True)
    digest = base64.b64encode(hashlib.sha256(key_blob).digest()).rstrip(b"=").decode("ascii")
    return f"SHA256:{digest}"


def generate_ed25519_key_pair(name: str) -> tuple[str, bytes]:
    """Generate an Ed25519 key pair and return its public key plus a ZIP archive."""
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        Encoding.PEM,
        PrivateFormat.OpenSSH,
        NoEncryption(),
    )
    public_key = private_key.public_key().public_bytes(
        Encoding.OpenSSH,
        PublicFormat.OpenSSH,
    ).decode("ascii")
    public_key = f"{public_key} {name}"

    archive = BytesIO()
    with ZipFile(archive, "w") as bundle:
        _write_archive_file(bundle, "id_ed25519", private_bytes, 0o600)
        _write_archive_file(bundle, "id_ed25519.pub", f"{public_key}\n".encode(), 0o644)
    return public_key, archive.getvalue()


def _write_archive_file(bundle: ZipFile, filename: str, content: bytes, mode: int) -> None:
    info = ZipInfo(filename)
    info.create_system = 3
    info.compress_type = ZIP_DEFLATED
    info.external_attr = mode << 16
    bundle.writestr(info, content)
