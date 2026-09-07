"""Offline regressions for persisted administrator and Telegram configuration."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class SystemConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # Import application settings only after unittest has discovered all modules.
        from config import settings
        from models.base import Base
        from models.oci_kv import OciKv
        from services.sys_service import SysService

        self.settings = settings
        self.OciKv = OciKv
        self.service = SysService
        self.runtime = tempfile.TemporaryDirectory(prefix="oci-helper-system-tests-")
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self.runtime.name) / 'system.db'}"
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all, tables=[OciKv.__table__])

        self.patches = ExitStack()
        self.patches.enter_context(patch.object(settings, "web_account", "admin"))
        self.patches.enter_context(patch.object(settings, "web_password", "bootstrap-password-123"))
        self.patches.enter_context(
            patch.object(settings, "data_encryption_key", "test-data-key-with-at-least-32-characters")
        )
        self.patches.enter_context(
            patch("services.sys_service._ADMIN_CREDENTIALS_LOCK", asyncio.Lock())
        )
        self.patches.enter_context(
            patch("services.sys_service._TELEGRAM_CONFIG_LOCK", asyncio.Lock())
        )

    async def asyncTearDown(self) -> None:
        self.patches.close()
        await self.engine.dispose()
        self.runtime.cleanup()

    async def test_concurrent_password_changes_recheck_current_credentials(self) -> None:
        from core.admin_credentials import load_admin_credentials, verify_admin_password
        from exceptions import OciException
        from schemas.sys_schemas import UpdateAdminCredentialsParams

        async def update_password(password: str):
            async with self.sessions() as db:
                await self.service.update_admin_credentials(
                    UpdateAdminCredentialsParams(
                        account="admin",
                        current_password="bootstrap-password-123",
                        new_password=password,
                    ),
                    db,
                )

        passwords = ["first-new-password-123", "second-new-password-123"]
        results = await asyncio.gather(
            *(update_password(password) for password in passwords), return_exceptions=True
        )
        successes = [index for index, result in enumerate(results) if result is None]
        self.assertEqual(len(successes), 1, results)
        failure = results[1 - successes[0]]
        self.assertIsInstance(failure, OciException)
        self.assertEqual(failure.message, "当前密码不正确")
        async with self.sessions() as db:
            rows = (await db.execute(select(self.OciKv))).scalars().all()
            self.assertEqual(len(rows), 3)
            credentials = await load_admin_credentials(db)
        self.assertTrue(verify_admin_password(passwords[successes[0]], credentials))

    async def test_concurrent_first_username_updates_do_not_duplicate_config_rows(self) -> None:
        from schemas.sys_schemas import UpdateAdminCredentialsParams

        async def update_account(account: str):
            async with self.sessions() as db:
                await self.service.update_admin_credentials(
                    UpdateAdminCredentialsParams(
                        account=account,
                        current_password="bootstrap-password-123",
                    ),
                    db,
                )

        await asyncio.gather(update_account("first-admin"), update_account("second-admin"))
        async with self.sessions() as db:
            codes = (await db.execute(select(self.OciKv.code))).scalars().all()
            self.assertEqual(len(codes), 3)
            self.assertEqual(len(set(codes)), 3)

    async def test_concurrent_telegram_updates_keep_runtime_and_database_consistent(self) -> None:
        from schemas.sys_schemas import UpdateSysCfgParams

        active_config = None

        async def initialize(token: str, chat_id: str):
            nonlocal active_config
            await asyncio.sleep(0)
            active_config = (token, chat_id)
            return object()

        async def update_config(token: str, chat_id: str):
            async with self.sessions() as db:
                await self.service.update_sys_cfg(
                    UpdateSysCfgParams(tg_bot_token=token, tg_chat_id=chat_id), db
                )

        with (
            patch("telegram_bot.init_telegram_bot", side_effect=initialize),
            patch("telegram_bot.shutdown_telegram_bot", new=AsyncMock()),
            patch("services.notification_service.requeue_waiting_notifications", new=AsyncMock()),
        ):
            await asyncio.gather(update_config("first-token", "1"), update_config("second-token", "2"))

        async with self.sessions() as db:
            token = await self.service._get_cfg_value(db, "SYS_TG_BOT_TOKEN")
            chat_id = await self.service._get_cfg_value(db, "SYS_TG_CHAT_ID")
            rows = (await db.execute(select(self.OciKv))).scalars().all()
        self.assertEqual(len(rows), 2)
        self.assertEqual(active_config, (token, chat_id))

    async def test_telegram_database_failure_restores_previous_configuration(self) -> None:
        from schemas.sys_schemas import UpdateSysCfgParams

        async with self.sessions() as db:
            await self.service._set_cfg_value(db, "SYS_TG_BOT_TOKEN", "original-token")
            await self.service._set_cfg_value(db, "SYS_TG_CHAT_ID", "original-chat")
            await db.commit()

        initialize = AsyncMock(return_value=object())
        async with self.sessions() as db:
            with (
                patch("telegram_bot.init_telegram_bot", initialize),
                patch("telegram_bot.shutdown_telegram_bot", new=AsyncMock()),
                patch("services.notification_service.requeue_waiting_notifications", new=AsyncMock()),
                patch.object(db, "commit", new=AsyncMock(side_effect=RuntimeError("write failed"))),
                self.assertRaisesRegex(RuntimeError, "write failed"),
            ):
                await self.service.update_sys_cfg(
                    UpdateSysCfgParams(tg_bot_token="new-token", tg_chat_id="new-chat"), db
                )
        self.assertEqual(initialize.await_args_list[-1].args, ("original-token", "original-chat"))
        async with self.sessions() as db:
            self.assertEqual(await self.service._get_cfg_value(db, "SYS_TG_BOT_TOKEN"), "original-token")

    def startup_patches(self) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(patch("main._configure_logging"))
        stack.enter_context(patch("main.init_db", new=AsyncMock()))
        stack.enter_context(patch("main._restore_tasks", new=AsyncMock()))
        stack.enter_context(patch("database.async_session", self.sessions))
        stack.enter_context(patch("core.task_scheduler.scheduler", MagicMock()))
        stack.enter_context(
            patch("services.notification_service.notification_dispatcher", MagicMock(stop=AsyncMock()))
        )
        stack.enter_context(patch("telegram_bot.init_telegram_bot", new=AsyncMock()))
        stack.enter_context(patch("telegram_bot.shutdown_telegram_bot", new=AsyncMock()))
        stack.enter_context(patch.object(self.settings, "web_password", ""))
        return stack

    async def test_persisted_admin_can_start_without_bootstrap_password(self) -> None:
        from core.admin_credentials import (
            ADMIN_ACCOUNT_CODE,
            ADMIN_AUTH_VERSION_CODE,
            ADMIN_PASSWORD_HASH_CODE,
            hash_admin_password,
        )
        from main import app, lifespan

        async with self.sessions() as db:
            for code, value in (
                (ADMIN_ACCOUNT_CODE, "persisted-admin"),
                (ADMIN_PASSWORD_HASH_CODE, hash_admin_password("persisted-password-123")),
                (ADMIN_AUTH_VERSION_CODE, "persisted-version"),
            ):
                await self.service._set_cfg_value(db, code, value)
            await db.commit()

        with self.startup_patches():
            async with lifespan(app):
                async with self.sessions() as db:
                    result = await self.service.get_sys_cfg(db)
                    self.assertEqual(result.admin_account, "persisted-admin")

    async def test_first_start_still_requires_strong_bootstrap_password(self) -> None:
        from main import app, lifespan

        with self.startup_patches(), self.assertRaisesRegex(RuntimeError, "OCI_HELPER_WEB_PASSWORD"):
            async with lifespan(app):
                self.fail("A fresh installation must reject an empty bootstrap password")


class RuntimeValidationTests(unittest.TestCase):
    def test_persisted_mode_still_checks_independent_secret_strength(self) -> None:
        from config import AppSettings

        for secret_field in ("jwt_secret", "data_encryption_key"):
            with self.subTest(secret_field=secret_field):
                values = {"web_password": "", "jwt_secret": "", "data_encryption_key": ""}
                values[secret_field] = "short"
                configured = AppSettings(_env_file=None, **values)
                with self.assertRaisesRegex(RuntimeError, "at least 32 characters"):
                    configured.validate_runtime(require_bootstrap_credentials=False)


if __name__ == "__main__":
    unittest.main()
