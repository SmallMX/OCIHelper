"""Small thread-safe in-process caches with per-entry TTL support."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any


class ExpireCache:
    def __init__(self, maxsize: int = 1024, ttl: int = 300):
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.RLock()
        self._maxsize = maxsize
        self._default_ttl = ttl

    def _purge_expired(self) -> None:
        now = time.monotonic()
        expired = [key for key, (deadline, _) in self._items.items() if deadline <= now]
        for key in expired:
            self._items.pop(key, None)

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            deadline, value = item
            if deadline <= time.monotonic():
                self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            return value

    def put(self, key: str, value: Any, ttl: int | None = None) -> None:
        effective_ttl = self._default_ttl if ttl is None else ttl
        if effective_ttl <= 0:
            raise ValueError("ttl must be greater than zero")
        with self._lock:
            self._purge_expired()
            self._items[key] = (time.monotonic() + effective_ttl, value)
            self._items.move_to_end(key)
            while len(self._items) > self._maxsize:
                self._items.popitem(last=False)

    def remove(self, key: str) -> Any | None:
        with self._lock:
            item = self._items.pop(key, None)
            return item[1] if item else None

    def contains(self, key: str) -> bool:
        return self.get(key) is not None

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def size(self) -> int:
        with self._lock:
            self._purge_expired()
            return len(self._items)


captcha_cache = ExpireCache(maxsize=100, ttl=300)
cache = ExpireCache(maxsize=500, ttl=3600)
