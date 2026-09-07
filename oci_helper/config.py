"""Application settings loaded from ``OCI_HELPER_*`` environment variables."""

from __future__ import annotations

import os
import secrets
import threading
import time
from pathlib import Path
from typing import Literal

from pydantic import Field, PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="OCI_HELPER_",
        extra="ignore",
    )

    server_host: str = "0.0.0.0"
    server_port: int = Field(default=8888, ge=1, le=65535)
    app_version: str = "0.0.2"

    web_account: str = Field(default="admin", min_length=1)
    web_password: str = ""
    jwt_secret: str = ""
    data_encryption_key: str = ""
    jwt_expire_hours: int = Field(default=24, ge=1, le=24 * 30)

    ip_ban_threshold: int = Field(default=5, ge=1, le=100)
    ip_ban_minutes: int = Field(default=30, ge=1, le=60 * 24 * 30)
    trust_proxy_headers: bool = False

    db_path: str = "./data/oci-helper.db"
    key_dir_path: str = "./keys"
    log_level: Literal["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_file_path: str = "./logs/oci-helper.log"

    cors_origins: str = ""
    task_worker_count: int = Field(default=8, ge=1, le=64)
    create_task_max_attempts: int = Field(default=1000, ge=0, le=100000)
    change_ip_interval_seconds: int = Field(default=10, ge=5, le=3600)
    change_ip_max_attempts: int = Field(default=120, ge=1, le=10000)
    oci_request_timeout_seconds: int = Field(default=120, ge=10, le=900)

    _secret_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _generated_secret: str | None = PrivateAttr(default=None)
    _generated_data_key: str | None = PrivateAttr(default=None)

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"

    @property
    def allowed_origins(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def effective_jwt_secret(self) -> str:
        """Return a stable signing key, persisting a generated key beside the DB."""
        if self.jwt_secret:
            return self.jwt_secret
        if self._generated_secret:
            return self._generated_secret

        with self._secret_lock:
            if self._generated_secret:
                return self._generated_secret
            secret = self._load_or_create_secret(".oci-helper-secret")
            if not secret:
                raise RuntimeError("JWT secret file is empty")
            self._generated_secret = secret
            return secret

    @property
    def effective_data_encryption_key(self) -> str:
        """Return a stable key dedicated to encrypting persisted credentials."""
        if self.data_encryption_key:
            return self.data_encryption_key
        if self._generated_data_key:
            return self._generated_data_key

        with self._secret_lock:
            if self._generated_data_key:
                return self._generated_data_key
            key = self._load_or_create_secret(".oci-helper-data-key")
            if not key:
                raise RuntimeError("data encryption key file is empty")
            self._generated_data_key = key
            return key

    def _load_or_create_secret(self, filename: str) -> str:
        db_path = Path(self.db_path).expanduser().resolve()
        secret_path = db_path.parent / filename
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        if secret_path.exists():
            value = secret_path.read_text(encoding="utf-8").strip()
            secret_path.chmod(0o600)
            return value

        value = secrets.token_urlsafe(48)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(secret_path, flags, 0o600)
        except FileExistsError as exc:
            for _ in range(20):
                value = secret_path.read_text(encoding="utf-8").strip()
                if value:
                    secret_path.chmod(0o600)
                    return value
                time.sleep(0.01)
            raise RuntimeError(
                f"secret file was created but remained empty: {secret_path}"
            ) from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
        return value

    def validate_runtime(self, *, require_bootstrap_credentials: bool = True) -> None:
        if require_bootstrap_credentials and (
            len(self.web_password) < 12
            or self.web_password.lower()
            in {
                "admin",
                "password",
                "change-me",
                "your_strong_password_here",
                "replace-with-at-least-12-characters",
            }
        ):
            raise RuntimeError(
                "OCI_HELPER_WEB_PASSWORD must be at least 12 characters and must not use a default password"
            )
        if self.jwt_secret and len(self.jwt_secret) < 32:
            raise RuntimeError("OCI_HELPER_JWT_SECRET must contain at least 32 characters")
        if self.data_encryption_key and len(self.data_encryption_key) < 32:
            raise RuntimeError("OCI_HELPER_DATA_ENCRYPTION_KEY must contain at least 32 characters")
        if "*" in self.allowed_origins:
            raise RuntimeError(
                "OCI_HELPER_CORS_ORIGINS must list explicit origins; wildcard is not allowed"
            )


settings = AppSettings()
