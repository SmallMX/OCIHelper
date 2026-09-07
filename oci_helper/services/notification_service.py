"""Durable, main-event-loop delivery for background Telegram notifications."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from core.secrets import decrypt_secret
from database import async_session
from models.notification_outbox import NotificationOutbox
from models.oci_kv import OciKv
from telegram_bot import get_bot, init_telegram_bot, send_notification
from utils.common import generate_id

NOTIFICATION_BATCH_SIZE = 20
NOTIFICATION_POLL_INTERVAL_SECONDS = 1.0
NOTIFICATION_UNCONFIGURED_DELAY_SECONDS = 60
NOTIFICATION_RETRY_BASE_SECONDS = 5
NOTIFICATION_RETRY_MAX_SECONDS = 3600
NOTIFICATION_MAX_ATTEMPTS = 20
NOTIFICATION_DELIVERY_TIMEOUT_SECONDS = 60
NOTIFICATION_LEASE_SECONDS = 120

NotificationSender = Callable[[str], Awaitable[bool]]


def _notification_due(now: datetime) -> ColumnElement[bool]:
    return and_(
        NotificationOutbox.status.in_(("pending", "sending")),
        or_(
            NotificationOutbox.next_attempt_at.is_(None),
            NotificationOutbox.next_attempt_at <= now,
        ),
    )


def _notification_lease(
    notification_id: str, lease_expires_at: datetime
) -> ColumnElement[bool]:
    return and_(
        NotificationOutbox.id == notification_id,
        NotificationOutbox.status == "sending",
        NotificationOutbox.next_attempt_at == lease_expires_at,
    )


def notification_retry_delay(attempts: int) -> int:
    exponent = max(0, min(attempts - 1, 16))
    return min(
        NOTIFICATION_RETRY_MAX_SECONDS,
        NOTIFICATION_RETRY_BASE_SECONDS * (2**exponent),
    )


async def enqueue_notification(
    db: AsyncSession,
    *,
    event_key: str,
    category: str,
    message: str,
) -> None:
    """Insert one idempotent notification without committing the caller's transaction."""
    now = datetime.now()
    statement = (
        sqlite_insert(NotificationOutbox)
        .values(
            id=generate_id(),
            event_key=event_key,
            category=category,
            message=message,
            status="pending",
            attempts=0,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["event_key"])
    )
    await db.execute(statement)


async def requeue_waiting_notifications(db: AsyncSession) -> int:
    """Retry pending/failed events immediately after Telegram configuration changes."""
    pending_result = await db.execute(
        update(NotificationOutbox)
        .where(NotificationOutbox.status == "pending")
        .values(next_attempt_at=None, last_error=None, updated_at=datetime.now())
    )
    failed_result = await db.execute(
        update(NotificationOutbox)
        .where(NotificationOutbox.status == "failed")
        .values(
            status="pending",
            attempts=0,
            next_attempt_at=None,
            last_error=None,
            updated_at=datetime.now(),
        )
    )
    return max(0, pending_result.rowcount or 0) + max(0, failed_result.rowcount or 0)


async def dispatch_due_notifications(
    *,
    session_factory: Any = async_session,
    sender: NotificationSender | None = None,
) -> int:
    """Attempt a bounded batch and return the number of events considered."""
    now = datetime.now()
    async with session_factory() as db:
        result = await db.execute(
            select(NotificationOutbox.id)
            .where(_notification_due(now))
            .order_by(NotificationOutbox.created_at, NotificationOutbox.id)
            .limit(NOTIFICATION_BATCH_SIZE)
        )
        notification_ids = list(result.scalars())

    uses_global_bot = sender is None
    resolved_sender = send_notification if sender is None else sender
    for notification_id in notification_ids:
        await _deliver_notification(
            notification_id,
            session_factory=session_factory,
            sender=resolved_sender,
            uses_global_bot=uses_global_bot,
        )
    return len(notification_ids)


async def _deliver_notification(
    notification_id: str,
    *,
    session_factory: Any,
    sender: NotificationSender,
    uses_global_bot: bool,
) -> None:
    now = datetime.now()
    lease_expires_at = now + timedelta(seconds=NOTIFICATION_LEASE_SECONDS)
    async with session_factory() as db:
        claimed = await db.execute(
            update(NotificationOutbox)
            .where(NotificationOutbox.id == notification_id, _notification_due(now))
            .values(status="sending", next_attempt_at=lease_expires_at, updated_at=now)
        )
        if claimed.rowcount != 1:
            return
        notification = await db.get(NotificationOutbox, notification_id)
        message = notification.message
        await db.commit()

    send_error: str | None = None
    try:
        async with asyncio.timeout(NOTIFICATION_DELIVERY_TIMEOUT_SECONDS):
            if uses_global_bot and get_bot() is None:
                if not await _try_restore_global_bot(session_factory):
                    await _defer_unavailable_notification(
                        notification_id, lease_expires_at, session_factory
                    )
                    return
            sent = await sender(message)
            if not sent:
                send_error = "Telegram send returned false"
    except Exception as exc:
        sent = False
        send_error = f"Telegram sender raised {type(exc).__name__}"
        logger.warning(
            "通知投递异常: notification={}, error_type={}",
            notification_id,
            type(exc).__name__,
        )

    completed_at = datetime.now()
    async with session_factory() as db:
        notification = (
            await db.execute(
                select(NotificationOutbox).where(
                    _notification_lease(notification_id, lease_expires_at)
                )
            )
        ).scalar_one_or_none()
        if notification is None:
            return
        attempts = notification.attempts + 1
        values = {
            "attempts": attempts,
            "updated_at": completed_at,
            "last_error": send_error,
            "next_attempt_at": None,
        }
        if sent:
            values.update(status="sent", last_error=None, sent_at=completed_at)
        elif attempts >= NOTIFICATION_MAX_ATTEMPTS:
            values["status"] = "failed"
        else:
            delay = notification_retry_delay(attempts)
            values.update(
                status="pending",
                next_attempt_at=completed_at + timedelta(seconds=delay),
            )
        completed = await db.execute(
            update(NotificationOutbox)
            .where(_notification_lease(notification_id, lease_expires_at))
            .values(**values)
        )
        await db.commit()
        if completed.rowcount != 1:
            return
        if sent:
            logger.info(
                "通知投递成功: notification={}, category={}, attempts={}",
                notification.id,
                notification.category,
                attempts,
            )
        elif attempts >= NOTIFICATION_MAX_ATTEMPTS:
            logger.error(
                "通知达到最大重试次数: notification={}, category={}, attempts={}",
                notification.id,
                notification.category,
                attempts,
            )
        else:
            logger.warning(
                "通知投递失败，稍后重试: notification={}, category={}, attempts={}, delay={}s",
                notification.id,
                notification.category,
                attempts,
                delay,
            )


async def _try_restore_global_bot(session_factory: Any) -> bool:
    if get_bot() is not None:
        return True
    async with session_factory() as db:
        result = await db.execute(
            select(OciKv.code, OciKv.value).where(
                OciKv.code.in_({"SYS_TG_BOT_TOKEN", "SYS_TG_CHAT_ID"})
            )
        )
        stored = dict(result.all())

    stored_token = stored.get("SYS_TG_BOT_TOKEN")
    chat_id = stored.get("SYS_TG_CHAT_ID")
    if not stored_token or not chat_id:
        return False
    try:
        bot_token = decrypt_secret(stored_token)
        return await init_telegram_bot(bot_token, chat_id) is not None
    except Exception as exc:
        logger.warning("恢复 Telegram Bot 失败: error_type={}", type(exc).__name__)
        return False


async def _defer_unavailable_notification(
    notification_id: str, lease_expires_at: datetime, session_factory: Any
) -> None:
    now = datetime.now()
    async with session_factory() as db:
        await db.execute(
            update(NotificationOutbox)
            .where(_notification_lease(notification_id, lease_expires_at))
            .values(
                status="pending",
                last_error="Telegram Bot is not configured or unavailable",
                next_attempt_at=now + timedelta(
                    seconds=NOTIFICATION_UNCONFIGURED_DELAY_SECONDS
                ),
                updated_at=now,
            )
        )
        await db.commit()


class NotificationDispatcher:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="notification-dispatcher")
        logger.info("通知投递器已启动")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._task = None
        self._stop_event = None
        logger.info("通知投递器已停止")

    async def _run(self) -> None:
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                attempted = await dispatch_due_notifications()
            except asyncio.CancelledError:
                raise
            except Exception:
                attempted = 0
                logger.exception("通知投递器执行异常")

            delay = 0.1 if attempted else NOTIFICATION_POLL_INTERVAL_SECONDS
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except TimeoutError:
                pass


notification_dispatcher = NotificationDispatcher()
