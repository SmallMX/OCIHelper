"""Persistent administrator credentials and password hashing helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.oci_kv import OciKv

ADMIN_ACCOUNT_CODE = "SYS_ADMIN_ACCOUNT"
ADMIN_PASSWORD_HASH_CODE = "SYS_ADMIN_PASSWORD_HASH"
ADMIN_AUTH_VERSION_CODE = "SYS_ADMIN_AUTH_VERSION"

_ENVIRONMENT_AUTH_VERSION = "environment"
_PASSWORD_HASH_SCHEME = "pbkdf2_sha256"
_PASSWORD_HASH_ITERATIONS = 600_000
_PASSWORD_HASH_BYTES = 32
_WEAK_PASSWORDS = {
    "admin",
    "password",
    "change-me",
    "your_strong_password_here",
    "replace-with-at-least-12-characters",
}


@dataclass(frozen=True)
class AdminCredentials:
    account: str
    password_hash: str | None
    auth_version: str
    is_persisted: bool


async def load_admin_credentials(db: AsyncSession) -> AdminCredentials:
    """Load the complete persisted credential set, or fall back to bootstrap settings."""
    codes = {
        ADMIN_ACCOUNT_CODE,
        ADMIN_PASSWORD_HASH_CODE,
        ADMIN_AUTH_VERSION_CODE,
    }
    result = await db.execute(select(OciKv.code, OciKv.value).where(OciKv.code.in_(codes)))
    values = {code: value for code, value in result.all() if value}
    if all(code in values for code in codes):
        return AdminCredentials(
            account=values[ADMIN_ACCOUNT_CODE],
            password_hash=values[ADMIN_PASSWORD_HASH_CODE],
            auth_version=values[ADMIN_AUTH_VERSION_CODE],
            is_persisted=True,
        )
    return AdminCredentials(
        account=settings.web_account,
        password_hash=None,
        auth_version=_ENVIRONMENT_AUTH_VERSION,
        is_persisted=False,
    )


def verify_admin_password(password: str, credentials: AdminCredentials) -> bool:
    if credentials.password_hash is None:
        return constant_time_equal(password, settings.web_password)
    return verify_password_hash(password, credentials.password_hash)


def hash_admin_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _PASSWORD_HASH_ITERATIONS,
        dklen=_PASSWORD_HASH_BYTES,
    )
    return "$".join(
        (
            _PASSWORD_HASH_SCHEME,
            str(_PASSWORD_HASH_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def verify_password_hash(password: str, encoded_hash: str) -> bool:
    try:
        scheme, iterations_text, salt_text, digest_text = encoded_hash.split("$", 3)
        iterations = int(iterations_text)
        if scheme != _PASSWORD_HASH_SCHEME or not 100_000 <= iterations <= 2_000_000:
            return False
        salt = base64.b64decode(salt_text, altchars=b"-_", validate=True)
        expected = base64.b64decode(digest_text, altchars=b"-_", validate=True)
        if len(salt) < 16 or len(expected) != _PASSWORD_HASH_BYTES:
            return False
    except (ValueError, TypeError):
        return False

    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
        dklen=len(expected),
    )
    return hmac.compare_digest(actual, expected)


def validate_admin_password(password: str) -> None:
    if len(password) < 12:
        raise ValueError("新密码至少需要 12 个字符")
    if password.lower() in _WEAK_PASSWORDS:
        raise ValueError("新密码不能使用常见默认密码")


def create_auth_version() -> str:
    return secrets.token_urlsafe(32)


def constant_time_equal(left: str, right: str) -> bool:
    """Compare arbitrary Unicode strings without leaking timing information."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
