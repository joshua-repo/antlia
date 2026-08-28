from __future__ import annotations

import threading
import time

import pytest

from antlia.auth import ratelimit
from antlia.auth.ratelimit import TokenBucket, Unlimited


def test_burst_then_throttle():
    bucket = TokenBucket(rate=100.0, burst=3)
    for _ in range(3):
        assert bucket.try_acquire()
    assert not bucket.try_acquire()


def test_refills_over_time():
    bucket = TokenBucket(rate=200.0, burst=1)
    assert bucket.try_acquire()
    start = time.monotonic()
    assert bucket.acquire()
    assert time.monotonic() - start >= 1 / 200 * 0.5


def test_timeout_returns_false_without_consuming():
    bucket = TokenBucket(rate=0.5, burst=1)
    assert bucket.try_acquire()
    assert not bucket.acquire(timeout=0.01)


def test_cannot_ask_for_more_than_the_bucket_holds():
    with pytest.raises(ValueError):
        TokenBucket(rate=1.0, burst=2).acquire(5)


def test_thread_safe_under_contention():
    bucket = TokenBucket(rate=1e6, burst=50)
    taken = []

    def worker():
        taken.append(bucket.try_acquire())

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(taken) == 50


def test_limiter_is_shared_per_identity():
    a = ratelimit.limiter_for("src", "live", rate=10.0)
    b = ratelimit.limiter_for("src", "live", rate=10.0)
    c = ratelimit.limiter_for("src", "paper", rate=10.0)
    assert a is b
    assert a is not c


def test_first_rate_wins_because_the_limit_is_the_vendors():
    first = ratelimit.limiter_for("src", None, rate=10.0)
    second = ratelimit.limiter_for("src", None, rate=999.0)
    assert second is first
    assert first.rate == 10.0


def test_no_declared_rate_means_unlimited():
    assert isinstance(ratelimit.limiter_for("src", None, rate=None), Unlimited)
