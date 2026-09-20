"""In-process cache for API business data.

Scope (project brief §31): caches API responses only. Credentials are never
cached here -- the transport holds the cookie for the process lifetime and
nothing is ever written to disk.

The cache is intentionally simple: a TTL map keyed by a string. It is not
shared between processes and never persists, which keeps the "no daemon, no
background refresh" guarantee (§32).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

T = TypeVar("T")

DEFAULT_TTL = 300.0


@dataclass
class _Entry(Generic[T]):
    value: T
    stored_at: float
    ttl: float

    def alive(self, now: float) -> bool:
        return (now - self.stored_at) < self.ttl


class Cache:
    """Thread-safe TTL cache."""

    def __init__(self, ttl: float = DEFAULT_TTL) -> None:
        self.ttl = ttl
        self._data: dict[str, _Entry[Any]] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        """Return a live entry, or ``None``."""
        now = time.time()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.misses += 1
                return None
            if not entry.alive(now):
                del self._data[key]
                self.misses += 1
                return None
            self.hits += 1
            return entry.value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        """Store ``value`` under ``key``."""
        with self._lock:
            self._data[key] = _Entry(
                value=value,
                stored_at=time.time(),
                ttl=self.ttl if ttl is None else ttl,
            )

    def get_or_set(
        self,
        key: str,
        factory: Callable[[], T],
        *,
        refresh: bool = False,
        ttl: float | None = None,
    ) -> T:
        """Return a cached value, computing it via ``factory`` when needed.

        ``refresh=True`` bypasses the cache read but still stores the result.
        """
        if not refresh:
            cached = self.get(key)
            if cached is not None:
                return cached
        value = factory()
        self.set(key, value, ttl=ttl)
        return value

    def invalidate(self, key: str | None = None) -> None:
        """Drop one entry, or every entry when ``key`` is ``None``."""
        with self._lock:
            if key is None:
                self._data.clear()
            else:
                self._data.pop(key, None)

    def clear(self) -> None:
        self.invalidate(None)

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._data)

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "size": self.size}
