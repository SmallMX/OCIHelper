"""Offline regression coverage for task cancellation and durable notification delivery."""

from __future__ import annotations

import asyncio
import importlib
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class DatabaseEventLoopTests(unittest.TestCase):
    def test_connections_remain_usable_across_worker_event_loops(self) -> None:
        from database import engine as application_engine

        with tempfile.TemporaryDirectory(prefix="oci-event-loop-tests-") as directory:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{Path(directory) / 'loop.db'}",
                poolclass=type(application_engine.pool),
                pool_pre_ping=True,
            )

            async def contend_for_connections():
                connections = []
                waiting = None
                try:
                    # Saturate the former default pool's five slots and ten overflow slots.
                    for _ in range(15):
                        connections.append(await engine.connect())

                    async def borrow_connection():
                        async with engine.connect():
                            pass

                    waiting = asyncio.create_task(borrow_connection())
                    try:
                        await asyncio.wait_for(asyncio.shield(waiting), timeout=0.05)
                    except TimeoutError:
                        waiting.cancel()
                        await asyncio.gather(waiting, return_exceptions=True)
                finally:
                    if waiting is not None and not waiting.done():
                        waiting.cancel()
                        await asyncio.gather(waiting, return_exceptions=True)
                    for connection in connections:
                        await connection.close()

            try:
                asyncio.run(contend_for_connections())
                asyncio.run(contend_for_connections())
            finally:
                asyncio.run(engine.dispose())


class TaskSchedulerReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        # Defer configuration imports until discovery has loaded the smoke-test environment.
        from core.task_scheduler import TaskScheduler

        self.scheduler = TaskScheduler(max_workers=1)
        self.releases: list[threading.Event] = []
        self.scheduler.start()

    def tearDown(self) -> None:
        self.scheduler.shutdown()
        for event in self.releases:
            event.set()
        self.wait_until(lambda: not self.scheduler._futures)

    def release_event(self) -> threading.Event:
        event = threading.Event()
        self.releases.append(event)
        return event

    def wait_until(self, predicate) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail("background operation did not reach the expected state")

    def test_waiting_tasks_do_not_fill_executor_or_count_as_attempts(self) -> None:
        entered = threading.Event()
        release = self.release_event()

        def block_worker():
            entered.set()
            release.wait(3)
            return True

        self.scheduler.submit_create_task("busy", block_worker, 1)
        self.assertTrue(entered.wait(3))
        callbacks = {str(index): Mock(return_value=True) for index in range(30)}
        for task_id, callback in callbacks.items():
            self.scheduler.submit_create_task(task_id, callback, 1)

        with self.scheduler._condition:
            self.assertEqual(len(self.scheduler._futures), 1)
            waiting = self.scheduler.get_running_create_tasks()
            self.assertTrue(all(not waiting[task_id].running for task_id in callbacks))
            self.assertTrue(all(waiting[task_id].execution_count == 0 for task_id in callbacks))
            for task_id in callbacks:
                self.scheduler.stop_create_task(task_id)

        release.set()
        self.wait_until(lambda: not self.scheduler._futures)
        for callback in callbacks.values():
            callback.assert_not_called()

    def test_pause_between_dispatch_and_execution_skips_callback(self) -> None:
        entered = threading.Event()
        release = self.release_event()
        execute = self.scheduler._execute
        callback = Mock(return_value=True)

        def delayed_execute(*args):
            entered.set()
            release.wait(3)
            return execute(*args)

        with patch.object(self.scheduler, "_execute", side_effect=delayed_execute):
            self.scheduler.submit_create_task("paused", callback, 1)
            self.assertTrue(entered.wait(3))
            task = self.scheduler.get_running_create_tasks()["paused"]
            self.assertEqual(task.execution_count, 0)
            self.assertTrue(self.scheduler.pause_create_task("paused"))
            release.set()
            self.wait_until(lambda: not task.running)
            callback.assert_not_called()
            self.assertEqual(task.execution_count, 0)
            self.assertTrue(self.scheduler.resume_create_task("paused"))
            self.wait_until(lambda: not self.scheduler.is_create_task_running("paused"))
        callback.assert_called_once()
        self.assertEqual(task.execution_count, 1)

    def test_stopped_execution_cannot_remove_a_replacement_with_the_same_key(self) -> None:
        entered = threading.Event()
        release = self.release_event()
        callback = Mock(return_value=True)

        def block_worker():
            entered.set()
            release.wait(3)
            return True

        self.scheduler.submit_create_task("same-key", block_worker, 1)
        self.assertTrue(entered.wait(3))
        self.assertTrue(self.scheduler.stop_create_task("same-key"))
        self.scheduler.submit_create_task("same-key", callback, 1, paused=True)
        replacement = self.scheduler.get_running_create_tasks()["same-key"]
        release.set()
        self.wait_until(lambda: not self.scheduler._futures)
        self.assertIs(self.scheduler.get_running_create_tasks()["same-key"], replacement)
        callback.assert_not_called()
        self.scheduler.resume_create_task("same-key")
        self.wait_until(lambda: not self.scheduler.is_create_task_running("same-key"))
        callback.assert_called_once()

    def test_restart_waits_for_old_workers_and_preserves_replacement(self) -> None:
        entered = threading.Event()
        release = self.release_event()
        callback = Mock(return_value=True)

        def block_worker():
            entered.set()
            release.wait(3)
            return True

        self.scheduler.submit_change_ip_task("config", "instance", block_worker)
        self.assertTrue(entered.wait(3))
        self.scheduler.shutdown()
        self.scheduler.start()
        self.scheduler.submit_change_ip_task("config", "instance", callback)
        with self.scheduler._condition:
            self.assertEqual(len(self.scheduler._futures), 1)
            self.assertFalse(self.scheduler._change_ip_tasks["config:instance"].running)
        callback.assert_not_called()
        release.set()
        self.wait_until(lambda: not self.scheduler.is_change_ip_running("config", "instance"))
        callback.assert_called_once()


class NotificationLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.service = importlib.import_module("services.notification_service")
        self.model = self.service.NotificationOutbox
        self.directory = tempfile.TemporaryDirectory(prefix="oci-notification-tests-")
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self.directory.name) / 'notifications.db'}"
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(self.model.__table__.create)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.deliveries: list[asyncio.Task] = []
        async with self.sessions() as db:
            await self.service.enqueue_notification(
                db, event_key="test-event", category="test", message="instance ready"
            )
            await db.commit()
            self.event_id = (await db.execute(select(self.model.id))).scalar_one()

    async def asyncTearDown(self) -> None:
        for task in self.deliveries:
            task.cancel()
        await asyncio.gather(*self.deliveries, return_exceptions=True)
        await self.engine.dispose()
        self.directory.cleanup()

    def start_delivery(self, sender) -> asyncio.Task:
        task = asyncio.create_task(
            self.service.dispatch_due_notifications(session_factory=self.sessions, sender=sender)
        )
        self.deliveries.append(task)
        return task

    async def get_event(self):
        async with self.sessions() as db:
            return await db.get(self.model, self.event_id)

    async def expire_lease(self) -> None:
        async with self.sessions() as db:
            await db.execute(
                update(self.model)
                .where(self.model.id == self.event_id)
                .values(next_attempt_at=datetime.now() - timedelta(seconds=1))
            )
            await db.commit()

    async def test_concurrent_dispatch_claims_an_event_only_once(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def send(_message):
            entered.set()
            await release.wait()
            return True

        sender = AsyncMock(side_effect=send)
        first = self.start_delivery(sender)
        second = self.start_delivery(sender)
        await asyncio.wait_for(entered.wait(), timeout=3)
        await asyncio.wait_for(
            asyncio.wait({first, second}, return_when=asyncio.FIRST_COMPLETED), timeout=3
        )
        sender.assert_awaited_once_with("instance ready")
        self.assertEqual((await self.get_event()).status, "sending")
        async with self.sessions() as db:
            self.assertEqual(await self.service.requeue_waiting_notifications(db), 0)
            await db.commit()
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=3)
        event = await self.get_event()
        self.assertEqual(event.status, "sent")
        self.assertEqual(event.attempts, 1)

    async def test_cancelled_delivery_recovers_after_the_lease_expires(self) -> None:
        entered = asyncio.Event()

        async def send(_message):
            entered.set()
            await asyncio.Event().wait()

        task = self.start_delivery(send)
        await asyncio.wait_for(entered.wait(), timeout=3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        sender = AsyncMock(return_value=True)
        self.assertEqual(
            await self.service.dispatch_due_notifications(
                session_factory=self.sessions, sender=sender
            ),
            0,
        )
        sender.assert_not_awaited()
        await self.expire_lease()
        await self.service.dispatch_due_notifications(session_factory=self.sessions, sender=sender)
        sender.assert_awaited_once()
        self.assertEqual((await self.get_event()).status, "sent")

    async def test_expired_delivery_cannot_overwrite_a_newer_claim(self) -> None:
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        first_release = asyncio.Event()
        second_release = asyncio.Event()

        async def first_send(_message):
            first_entered.set()
            await first_release.wait()
            return True

        async def second_send(_message):
            second_entered.set()
            await second_release.wait()
            return False

        first = self.start_delivery(first_send)
        await asyncio.wait_for(first_entered.wait(), timeout=3)
        await self.expire_lease()
        second = self.start_delivery(second_send)
        await asyncio.wait_for(second_entered.wait(), timeout=3)
        current_lease = (await self.get_event()).next_attempt_at
        first_release.set()
        await asyncio.wait_for(first, timeout=3)
        event = await self.get_event()
        self.assertEqual(event.status, "sending")
        self.assertEqual(event.next_attempt_at, current_lease)
        self.assertEqual(event.attempts, 0)
        second_release.set()
        await asyncio.wait_for(second, timeout=3)
        event = await self.get_event()
        self.assertEqual(event.status, "pending")
        self.assertEqual(event.attempts, 1)
        self.assertIsNone(event.sent_at)

    async def test_sender_timeout_releases_claim_for_a_bounded_retry(self) -> None:
        async def send(_message):
            await asyncio.Event().wait()

        with patch.object(self.service, "NOTIFICATION_DELIVERY_TIMEOUT_SECONDS", 0.01):
            await asyncio.wait_for(
                self.service.dispatch_due_notifications(
                    session_factory=self.sessions, sender=send
                ),
                timeout=3,
            )
        event = await self.get_event()
        self.assertEqual(event.status, "pending")
        self.assertEqual(event.attempts, 1)
        self.assertIn("TimeoutError", event.last_error)
        self.assertGreater(event.next_attempt_at, datetime.now())
