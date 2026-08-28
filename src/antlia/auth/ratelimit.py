"""A token bucket per source/profile, shared by every session of that identity.

Rate limits belong to the account, not to the caller. Two parts of a program
holding two handles to the same ThetaData account are one client as far as the
vendor is concerned, so the bucket is keyed by identity and looked up from a
process-wide table rather than constructed per session.

This is the piece consumers most often reimplement badly, which is exactly why
it lives below the adapters instead of in each of them.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


class TokenBucket:
    """Classic token bucket. `acquire` blocks until the tokens are available."""

    def __init__(self, rate: float, burst: int = 1) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = float(rate)
        self.burst = max(1, int(burst))
        self._tokens = float(self.burst)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self, now: float) -> None:
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
            self._updated = now

    def acquire(self, tokens: float = 1.0, timeout: float | None = None) -> bool:
        """Consume `tokens`, sleeping as needed. False iff `timeout` expired."""
        if tokens > self.burst:
            raise ValueError(f"cannot acquire {tokens} from a bucket of depth {self.burst}")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._refill(now)
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True
                wait = (tokens - self._tokens) / self.rate
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait = min(wait, remaining)
            time.sleep(wait)

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Consume `tokens` if available right now; never sleeps."""
        with self._lock:
            self._refill(time.monotonic())
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def __repr__(self) -> str:
        return f"TokenBucket(rate={self.rate}/s, burst={self.burst})"


class Unlimited:
    """Null object for sources that declare no rate. Same shape, no waiting."""

    rate = float("inf")
    burst = 1

    def acquire(self, tokens: float = 1.0, timeout: float | None = None) -> bool:
        return True

    def try_acquire(self, tokens: float = 1.0) -> bool:
        return True

    def __repr__(self) -> str:
        return "Unlimited()"


Limiter = TokenBucket | Unlimited

_buckets: dict[str, Limiter] = {}
_buckets_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class LimiterKey:
    source: str
    profile: str | None = None

    def __str__(self) -> str:
        return self.source if self.profile is None else f"{self.source}:{self.profile}"


def limiter_for(source: str, profile: str | None, rate: float | None, burst: int = 1) -> Limiter:
    """The process-wide limiter for one identity, created on first request.

    The first caller's rate wins; a later caller asking for a different rate on
    the same identity gets the existing bucket, because the vendor's limit is
    not a per-caller preference.
    """
    key = str(LimiterKey(source, profile))
    with _buckets_lock:
        existing = _buckets.get(key)
        if existing is not None:
            return existing
        created: Limiter = Unlimited() if rate is None else TokenBucket(rate, burst)
        _buckets[key] = created
        return created


def reset() -> None:
    """Drop every bucket. For tests."""
    with _buckets_lock:
        _buckets.clear()
