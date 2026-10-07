"""The TTL, the stale copy, and the trap that made the cache never hit."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from antlia.live.fx import cache
from antlia.live.fx.types import PIVOT, RateTable

RATES = {"USD": 1.0, "GBP": 0.73869}


def table(**kwargs) -> RateTable:
    return RateTable(
        base=PIVOT,
        rates=RATES,
        as_of=kwargs.pop("as_of", datetime(2026, 8, 28, 0, 0, tzinfo=UTC)),
        source=kwargs.pop("source", "yfinance"),
        **kwargs,
    )


def test_cache_lives_under_antlia_home(tmp_path):
    assert cache.path() == tmp_path / "fx.json"


def test_round_trip():
    cache.write(table())
    entry = cache.read()
    assert entry is not None
    assert entry.table.rates == RATES
    assert entry.table.source == "yfinance"
    assert entry.table.as_of == datetime(2026, 8, 28, 0, 0, tzinfo=UTC)


def test_ttl_is_measured_from_the_fetch_not_from_as_of():
    # The bug this test exists for: Yahoo stamps an FX rate with the last daily
    # close, so `as_of` is already ~20h old by lunchtime. A TTL keyed to it
    # expires immediately and the cache never once hits.
    cache.write(table(as_of=datetime(2020, 1, 1, tzinfo=UTC)))
    entry = cache.read()
    assert entry is not None
    assert entry.table.age > timedelta(days=365)
    assert entry.age < timedelta(seconds=5)
    assert cache.usable(entry, RATES, ttl=timedelta(hours=6))


def test_an_old_fetch_is_not_usable():
    cache.write(table())
    raw = json.loads(cache.path().read_text())
    raw["fetched_at"] = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
    cache.path().write_text(json.dumps(raw))
    entry = cache.read()
    assert entry is not None
    assert not cache.usable(entry, RATES)
    # Still covers the currencies, which is what the stale fallback needs.
    assert cache.covers(entry, RATES)


def test_a_missing_currency_is_never_usable():
    cache.write(table())
    entry = cache.read()
    assert entry is not None
    assert not cache.covers(entry, ("USD", "GBP", "CHF"))
    assert not cache.usable(entry, ("USD", "GBP", "CHF"))


def test_a_file_without_fetched_at_falls_back_to_as_of():
    cache.write(table(as_of=datetime.now(UTC)))
    raw = json.loads(cache.path().read_text())
    del raw["fetched_at"]
    cache.path().write_text(json.dumps(raw))
    entry = cache.read()
    assert entry is not None
    assert entry.age < timedelta(seconds=5)


def test_absent_and_corrupt_caches_are_simply_absent():
    assert cache.read() is None
    cache.path().parent.mkdir(parents=True, exist_ok=True)
    cache.path().write_text("{not json")
    assert cache.read() is None
    cache.path().write_text('{"base": "USD"}')
    assert cache.read() is None


def test_write_leaves_no_temp_file_behind():
    cache.write(table())
    assert [p.name for p in cache.path().parent.iterdir()] == ["fx.json"]


def test_write_survives_an_unwritable_directory(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr("pathlib.Path.write_text", boom)
    cache.write(table())  # a cache that cannot be written is not an error
    assert cache.read() is None


def test_clear_is_idempotent():
    cache.write(table())
    cache.clear()
    cache.clear()
    assert cache.read() is None
