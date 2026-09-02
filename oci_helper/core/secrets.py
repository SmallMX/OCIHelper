"""Encryption helpers for persisted application credentials."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from config import settings

_PREFIX = "enc:v1:"


def _fernet() -> Fernet:
    digest = hashlib.sha256(
        f"oci-helper-data-secret:{settings.effective_data_encryption_key}".encode()
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    if value.startswith(_PREFIX):
        return value
    token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return f"{_PREFIX}{token}"


def decrypt_secret(value: str | None) -> str:
    if not value:
        return ""
    if not value.startswith(_PREFIX):
        # Legacy plaintext rows are accepted once and encrypted by startup migration.
        return value
    try:
        return _fernet().decrypt(value[len(_PREFIX) :].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError(
            "Unable to decrypt persisted credential; check "
            "OCI_HELPER_DATA_ENCRYPTION_KEY or .oci-helper-data-key"
        ) from exc


def is_encrypted(value: str | None) -> bool:
    return bool(value and value.startswith(_PREFIX))
