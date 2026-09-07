"""Bounded background scheduler used for OCI retry tasks.

One dispatcher thread tracks all timers.  OCI calls run in a bounded worker
pool, so sleeping periodic tasks do not permanently consume worker threads.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass

from loguru import logger

from config import settings


@dataclass(frozen=True)
class TaskExecutionResult:
    done: bool = False
    next_delay: float | None = None


class TaskInfo:
    def __init__(
        self,
        task_id: str,
        interval: int,
        paused: bool = False,
        initial_delay: float = 0,
    ):
        self.task_id = task_id
        self.interval = max(1, interval)
        self.paused = paused
        self.execution_count = 0
        self.running = False
        self.stopped = False
        self.next_run = time.monotonic() + max(0, initial_delay)


class TaskScheduler:
    def __init__(self, max_workers: int | None = None):
        self._worker_count = max_workers or settings.task_worker_count
        self._executor: ThreadPoolExecutor | None = None
        self._create_tasks: dict[str, TaskInfo] = {}
        self._change_ip_tasks: dict[str, TaskInfo] = {}
        self._functions: dict[tuple[str, str], Callable[[], object]] = {}
        self._futures: set[Future] = set()
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._running = False
        self._dispatcher: threading.Thread | None = None

    def start(self) -> None:
        """Start the dispatcher lazily as part of the application lifespan."""
        with self._condition:
            if self._running:
                return
            self._executor = ThreadPoolExecutor(
                max_workers=self._worker_count,
                thread_name_prefix="oci-worker",
            )
            self._running = True
            self._dispatcher = threading.Thread(
                target=self._dispatch_loop,
                args=(self._executor,),
                name="oci-task-dispatcher",
                daemon=True,
            )
            self._dispatcher.start()
        logger.info("任务调度器已启动，工作线程数: {}", self._worker_count)

    @staticmethod
    def _change_key(oci_cfg_id: str, instance_id: str) -> str:
        return f"{oci_cfg_id}:{instance_id}"

    def submit_create_task(
        self,
        task_id: str,
        func: Callable[[], object],
        interval: int,
        *,
        paused: bool = False,
        initial_delay: float = 0,
    ) -> None:
        self._submit("create", task_id, func, interval, paused, initial_delay)

    def submit_change_ip_task(
        self,
        oci_cfg_id: str,
        instance_id: str,
        func: Callable[[], object],
        interval: int = 10,
        *,
        paused: bool = False,
    ) -> str:
        task_key = self._change_key(oci_cfg_id, instance_id)
        self._submit("change_ip", task_key, func, interval, paused, 0)
        return task_key

    def _submit(
        self,
        category: str,
        task_key: str,
        func: Callable[[], object],
        interval: int,
        paused: bool,
        initial_delay: float,
    ) -> None:
        registry = self._registry(category)
        with self._condition:
            if not self._running:
                raise RuntimeError("task scheduler is shut down")
            if task_key in registry:
                logger.warning("任务已存在，跳过重复提交: {}", task_key)
                return
            registry[task_key] = TaskInfo(task_key, interval, paused, initial_delay)
            self._functions[(category, task_key)] = func
            self._condition.notify_all()
        logger.info("已提交{}任务: {}，间隔 {} 秒", category, task_key, interval)

    def stop_create_task(self, task_id: str) -> bool:
        return self._stop("create", task_id)

    def stop_change_ip_task(self, oci_cfg_id: str, instance_id: str) -> bool:
        return self._stop("change_ip", self._change_key(oci_cfg_id, instance_id))

    def stop_change_ip_by_instance(self, instance_id: str) -> bool:
        with self._lock:
            keys = [key for key in self._change_ip_tasks if key.rsplit(":", 1)[-1] == instance_id]
        stopped = False
        for key in keys:
            stopped = self._stop("change_ip", key) or stopped
        return stopped

    def _stop(self, category: str, task_key: str) -> bool:
        registry = self._registry(category)
        with self._condition:
            task = registry.pop(task_key, None)
            self._functions.pop((category, task_key), None)
            if task is None:
                return False
            task.stopped = True
            self._condition.notify_all()
        logger.info("已停止任务: {}", task_key)
        return True

    def pause_create_task(self, task_id: str) -> bool:
        return self._set_paused(task_id, True)

    def resume_create_task(self, task_id: str) -> bool:
        return self._set_paused(task_id, False)

    def _set_paused(self, task_id: str, paused: bool) -> bool:
        with self._condition:
            task = self._create_tasks.get(task_id)
            if task is None:
                return False
            task.paused = paused
            if not paused:
                task.next_run = time.monotonic()
            self._condition.notify_all()
        logger.info("已{}创建任务: {}", "暂停" if paused else "恢复", task_id)
        return True

    def is_create_task_running(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._create_tasks

    def is_change_ip_running(self, oci_cfg_id: str, instance_id: str) -> bool:
        with self._lock:
            return self._change_key(oci_cfg_id, instance_id) in self._change_ip_tasks

    def get_running_create_tasks(self) -> dict[str, TaskInfo]:
        with self._lock:
            return dict(self._create_tasks)

    def get_task_count(self, task_id: str) -> int:
        with self._lock:
            task = self._create_tasks.get(task_id)
            return task.execution_count if task else 0

    def shutdown(self) -> None:
        with self._condition:
            if not self._running:
                return
            self._running = False
            for task in (*self._create_tasks.values(), *self._change_ip_tasks.values()):
                task.stopped = True
            self._create_tasks.clear()
            self._change_ip_tasks.clear()
            self._functions.clear()
            self._condition.notify_all()
            dispatcher = self._dispatcher
            executor = self._executor
            self._dispatcher = None
            self._executor = None
        if dispatcher is not None:
            dispatcher.join(timeout=5)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        logger.info("任务调度器已关闭")

    def _registry(self, category: str) -> dict[str, TaskInfo]:
        return self._create_tasks if category == "create" else self._change_ip_tasks

    def _dispatch_loop(self, executor: ThreadPoolExecutor) -> None:
        while True:
            due: list[tuple[str, str, TaskInfo, Callable[[], object]]] = []
            with self._condition:
                if not self._running or self._executor is not executor:
                    return
                available_workers = self._worker_count - len(self._futures)
                if available_workers <= 0:
                    self._condition.wait(timeout=1.0)
                    continue
                now = time.monotonic()
                next_deadline: float | None = None
                for category, registry in (
                    ("create", self._create_tasks),
                    ("change_ip", self._change_ip_tasks),
                ):
                    for task_key, task in list(registry.items()):
                        if task.stopped or task.paused or task.running:
                            continue
                        if task.next_run <= now:
                            func = self._functions.get((category, task_key))
                            if func is not None:
                                due.append((category, task_key, task, func))
                        else:
                            next_deadline = (
                                task.next_run
                                if next_deadline is None
                                else min(next_deadline, task.next_run)
                            )

                if not due:
                    timeout = 1.0
                    if next_deadline is not None:
                        timeout = max(0.05, min(1.0, next_deadline - now))
                    self._condition.wait(timeout=timeout)
                    continue

                # Keep waiting work in the timer registry, not the executor's unbounded queue.
                due.sort(key=lambda entry: entry[2].next_run)
                for category, task_key, task, func in due[:available_workers]:
                    task.running = True
                    try:
                        future = executor.submit(self._execute, category, task_key, task, func)
                    except RuntimeError:
                        task.running = False
                        if not self._running:
                            task.stopped = True
                        return
                    self._futures.add(future)
                    future.add_done_callback(
                        lambda completed, c=category, k=task_key, t=task: self._complete(
                            c, k, t, completed
                        )
                    )

    def _execute(
        self,
        category: str,
        task_key: str,
        task: TaskInfo,
        func: Callable[[], object],
    ) -> object:
        with self._condition:
            if (
                not self._running
                or self._registry(category).get(task_key) is not task
                or task.stopped
                or task.paused
            ):
                return TaskExecutionResult()
            task.execution_count += 1
        return func()

    def _complete(self, category: str, task_key: str, task: TaskInfo, future: Future) -> None:
        result = TaskExecutionResult()
        try:
            returned = future.result()
            if isinstance(returned, TaskExecutionResult):
                result = returned
            elif returned is True:
                result = TaskExecutionResult(done=True)
        except CancelledError:
            pass
        except Exception:
            logger.exception("任务执行异常: {}", task_key)

        registry = self._registry(category)
        with self._condition:
            self._futures.discard(future)
            self._condition.notify_all()
            if registry.get(task_key) is not task:
                return
            task.running = False
            if result.done or task.stopped:
                registry.pop(task_key, None)
                self._functions.pop((category, task_key), None)
            else:
                delay = task.interval if result.next_delay is None else max(1, result.next_delay)
                task.next_run = time.monotonic() + delay
            self._condition.notify_all()


scheduler = TaskScheduler()
