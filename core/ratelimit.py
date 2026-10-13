"""In-process sliding-window rate limiter.

This complements (does not replace) Supabase Auth's own server-side rate limits.
It is per app instance; on a single Render instance that is the whole app.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, max_events: int, window_seconds: float, clock=time.monotonic):
        self.max_events = max_events
        self.window = window_seconds
        self.clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._events[key]
        while q and now - q[0] >= self.window:
            q.popleft()
        return q

    def allow(self, key: str) -> bool:
        """Record an attempt; False if the key is over its limit (attempt not recorded)."""
        with self._lock:
            now = self.clock()
            q = self._prune(key, now)
            if len(q) >= self.max_events:
                return False
            q.append(now)
            if len(self._events) > 50_000:  # bound memory under abuse
                for k in [k for k, v in self._events.items() if not v][:10_000]:
                    del self._events[k]
            return True

    def retry_after(self, key: str) -> int:
        with self._lock:
            q = self._prune(key, self.clock())
            if len(q) < self.max_events:
                return 0
            return max(1, int(self.window - (self.clock() - q[0])) + 1)

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)
