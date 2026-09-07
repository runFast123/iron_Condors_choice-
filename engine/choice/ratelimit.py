"""Thread-safe token bucket.

``kkunal`` implements no throttling whatsoever — no rate limiting, no 429
handling, no ``Retry-After`` support.  Hammering ChartData while backfilling a
few hundred option legs is a reliable way to get throttled, and because the
upstream library converts every failure into an empty DataFrame you would never
find out.  This bucket paces every outbound call instead.
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, rate: float, burst: float | None = None) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = float(rate)
        self.capacity = float(burst if burst is not None else max(1.0, rate))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> float:
        """Block until ``tokens`` are available. Returns seconds spent waiting."""
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                sleep_for = deficit / self.rate
            time.sleep(sleep_for)
            waited += sleep_for

    def penalise(self, seconds: float) -> None:
        """Drain the bucket for ``seconds`` after a 429, so we back off globally."""
        with self._lock:
            self._tokens = min(self._tokens, 0.0) - max(0.0, seconds) * self.rate
