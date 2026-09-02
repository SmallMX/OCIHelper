"""Authentication, Telegram configuration and dashboard use cases."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from core.admin_credentials import (
    ADMIN_ACCOUNT_CODE,
    ADMIN_AUTH_VERSION_CODE,
    ADMIN_PASSWORD_HASH_CODE,
    constant_time_equal,
    create_auth_version,
    hash_admin_password,
    load_admin_credentials,
    validate_admin_password,
    verify_admin_password,
)
from core.auth import create_jwt_token, ip_security
from core.secrets import decrypt_secret, encrypt_secret
from exceptions import OciException
from models.oci_create_task import OciCreateTask
from models.oci_kv import OciKv
from models.oci_user import OciUser
from schemas.sys_schemas import (
    GetGlanceRsp,
    GetSysCfgRsp,
    GetTaskLogsParams,
    GetTaskLogsRsp,
    LoginParams,
    LoginRsp,
    UpdateAdminCredentialsParams,
    UpdateSysCfgParams,
)
from utils.common import generate_id

_STARTED_AT = datetime.now()
_LOG_TAIL_MAX_BYTES = 2 * 1024 * 1024


class SysService:
    @staticmethod
    async def login(params: LoginParams, client_ip: str, db: AsyncSession) -> LoginRsp:
        if ip_security.is_banned(client_ip):
            raise OciException(-1, "账号或密码不正确")
        credentials = await load_admin_credentials(db)
        account_ok = constant_time_equal(params.account, credentials.account)
        password_ok = await asyncio.to_thread(verify_admin_password, params.password, credentials)
        if not account_ok or not password_ok:
            ip_security.record_failure(client_ip)
            logger.warning("登录失败: client_ip={}", client_ip)
            raise OciException(-1, "账号或密码不正确")

        ip_security.record_success(client_ip)
        token = create_jwt_token(params.account, credentials.auth_version)
        logger.info("登录成功: client_ip={}, account={}", client_ip, params.account)
        return LoginRsp(token=token, current_version=settings.app_version)

    @staticmethod
    async def get_sys_cfg(db: AsyncSession) -> GetSysCfgRsp:
        token = await SysService._get_cfg_value(db, "SYS_TG_BOT_TOKEN")
        credentials = await load_admin_credentials(db)
        return GetSysCfgRsp(
            admin_account=credentials.account,
            tg_chat_id=await SysService._get_cfg_value(db, "SYS_TG_CHAT_ID"),
            tg_bot_configured=bool(token),
        )

    @staticmethod
    async def update_admin_credentials(
        params: UpdateAdminCredentialsParams, db: AsyncSession
    ) -> None:
        credentials = await load_admin_credentials(db)
        password_ok = await asyncio.to_thread(
            verify_admin_password, params.current_password, credentials
        )
        if not password_ok:
            raise OciException(-1, "当前密码不正确")
        if params.account == credentials.account and params.new_password is None:
            raise OciException(-1, "管理员用户名或密码未发生变化")

        if params.new_password is not None:
            try:
                validate_admin_password(params.new_password)
            except ValueError as exc:
                raise OciException(-1, str(exc)) from exc
            password_hash = await asyncio.to_thread(hash_admin_password, params.new_password)
        elif credentials.password_hash is not None:
            password_hash = credentials.password_hash
        else:
            password_hash = await asyncio.to_thread(hash_admin_password, settings.web_password)

        try:
            await SysService._set_cfg_value(db, ADMIN_ACCOUNT_CODE, params.account)
            await SysService._set_cfg_value(db, ADMIN_PASSWORD_HASH_CODE, password_hash)
            await SysService._set_cfg_value(db, ADMIN_AUTH_VERSION_CODE, create_auth_version())
            await db.commit()
        except Exception:
            await db.rollback()
            raise

        logger.info(
            "管理员凭据已更新: account_changed={}, password_changed={}",
            params.account != credentials.account,
            params.new_password is not None,
        )

    @staticmethod
    async def update_sys_cfg(params: UpdateSysCfgParams, db: AsyncSession) -> None:
        from services.notification_service import requeue_waiting_notifications
        from telegram_bot import init_telegram_bot, shutdown_telegram_bot

        old_token = await SysService._get_cfg_value(db, "SYS_TG_BOT_TOKEN")
        old_chat_id = await SysService._get_cfg_value(db, "SYS_TG_CHAT_ID")
        if params.tg_bot_token and params.tg_chat_id:
            notifier = await init_telegram_bot(params.tg_bot_token, params.tg_chat_id)
            if notifier is None:
                raise OciException(-1, "Telegram Bot 配置验证失败，原配置未更改")

        try:
            await SysService._set_cfg_value(db, "SYS_TG_BOT_TOKEN", params.tg_bot_token or "")
            await SysService._set_cfg_value(db, "SYS_TG_CHAT_ID", params.tg_chat_id or "")
            if params.tg_bot_token and params.tg_chat_id:
                await requeue_waiting_notifications(db)
            await db.commit()
        except Exception:
            await db.rollback()
            if old_token and old_chat_id:
                await init_telegram_bot(old_token, old_chat_id)
            else:
                await shutdown_telegram_bot()
            raise

        if not params.tg_bot_token:
            await shutdown_telegram_bot()

    @staticmethod
    async def glance(db: AsyncSession) -> GetGlanceRsp:
        user_count = (await db.execute(select(func.count()).select_from(OciUser))).scalar() or 0
        task_count = (
            await db.execute(
                select(func.count())
                .select_from(OciCreateTask)
                .where(OciCreateTask.status.in_({"pending", "running", "paused"}))
            )
        ).scalar() or 0
        region_count = (
            await db.execute(select(func.count(OciUser.oci_region.distinct())))
        ).scalar() or 0
        uptime_days = (datetime.now() - _STARTED_AT).days
        return GetGlanceRsp(
            users=str(user_count),
            tasks=str(task_count),
            regions=str(region_count),
            days=str(uptime_days),
            current_version=settings.app_version,
        )

    @staticmethod
    async def get_task_logs(params: GetTaskLogsParams) -> GetTaskLogsRsp:
        log_path = Path(settings.log_file_path).expanduser().resolve()
        return await asyncio.to_thread(_read_task_log_tail, log_path, params.lines)

    @staticmethod
    async def _get_cfg_value(db: AsyncSession, code: str) -> str | None:
        result = await db.execute(select(OciKv.value).where(OciKv.code == code))
        value = result.scalar_one_or_none()
        if code == "SYS_TG_BOT_TOKEN" and value:
            return decrypt_secret(value)
        return value

    @staticmethod
    async def _set_cfg_value(db: AsyncSession, code: str, value: str) -> None:
        stored_value = encrypt_secret(value) if code == "SYS_TG_BOT_TOKEN" else value
        result = await db.execute(select(OciKv).where(OciKv.code == code))
        item = result.scalar_one_or_none()
        if item is None:
            db.add(OciKv(id=generate_id(), code=code, type="SYS_INIT_CFG", value=stored_value))
        else:
            item.value = stored_value


def _read_task_log_tail(path: Path, line_count: int) -> GetTaskLogsRsp:
    try:
        with path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            start = max(0, stat.st_size - _LOG_TAIL_MAX_BYTES)
            stream.seek(start)
            payload = stream.read(_LOG_TAIL_MAX_BYTES)
            if start > 0:
                stream.seek(start - 1)
                starts_at_line_boundary = stream.read(1) == b"\n"
                if not starts_at_line_boundary:
                    first_newline = payload.find(b"\n")
                    payload = payload[first_newline + 1 :] if first_newline >= 0 else b""
    except FileNotFoundError:
        return GetTaskLogsRsp()
    except OSError as exc:
        logger.warning("读取任务日志失败: error_type={}", type(exc).__name__)
        raise OciException(-1, "任务日志暂时不可读取") from exc

    all_lines = payload.decode("utf-8", errors="replace").splitlines()
    lines = all_lines[-line_count:]
    return GetTaskLogsRsp(
        lines=lines,
        line_count=len(lines),
        truncated=start > 0 or len(all_lines) > line_count,
        updated_at=datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    )
