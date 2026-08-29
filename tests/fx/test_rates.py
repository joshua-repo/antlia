"""The chain: order, fallback, the stale copy, and when it raises."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from antlia import fx
from antlia.auth.errors import ConnectionFailed, MissingExtra, UnknownSource
from antlia.fx import cache
from tests.fx.fakes import FakeRates

MARKET = {"USD": 1.0, "GBP": 0.73869, "JPY": 160.038}
FIXING = {"USD": 1.0, "GBP": 0.73624, "JPY": 159.68}


def only(*sources: FakeRates) -> None:
    """Replace the built-in chain, preserving the order given."""
    for name in fx.chain():
        fx.unregister(name)
    for source in sources:
        fx.register(source.name, source)


def test_the_builtin_chain_prefers_the_market_rate_over_the_fixing():
    # Order is policy, not alphabetical: yfinance is a market rate that
    # reconciles with the broker, frankfurter is a once-daily 16:00 CET fixing.
    assert fx.chain() == ["yfinance", "frankfurter"]


def test_the_first_source_that_answers_wins():
    primary, fallback = FakeRates("primary", MARKET), FakeRates("fallback", FIXING)
    only(primary, fallback)
    table = fx.rates(("USD", "GBP"))
    assert table.source == "primary"
    assert table.rate("GBP") == 0.73869
    assert fallback.calls == 0


def test_a_failed_source_falls_through_to_the_next():
    primary = FakeRates("primary", fail=ConnectionFailed("primary", "throttled"))
    fallback = FakeRates("fallback", FIXING)
    only(primary, fallback)
    table = fx.rates(("USD", "GBP"))
    assert table.source == "fallback"
    assert table.rate("GBP") == 0.73624


def test_a_missing_extra_falls_through_rather_than_ending_the_chain():
    primary = FakeRates("primary", fail=MissingExtra("primary", "yfinance", "yfinance"))
    only(primary, FakeRates("fallback", FIXING))
    assert fx.rates(("USD", "GBP")).source == "fallback"


def test_an_adapter_bug_falls_through_too():
    # The whole point of a second source is that the first one may be broken in
    # a way nobody anticipated -- including a raw TypeError from a third-party
    # adapter. It is recorded, not allowed to end the chain.
    primary = FakeRates("primary", fail=TypeError("adapter bug"))
    only(primary, FakeRates("fallback", FIXING))
    assert fx.rates(("USD", "GBP")).source == "fallback"


def test_every_failure_is_named_when_nothing_works():
    only(
        FakeRates("primary", fail=ConnectionFailed("primary", "throttled")),
        FakeRates("fallback", fail=TypeError("adapter bug")),
    )
    with pytest.raises(ConnectionFailed) as caught:
        fx.rates(("USD", "GBP"))
    message = str(caught.value)
    assert "throttled" in message and "adapter bug" in message


def test_it_raises_rather_than_returning_nothing():
    # Degrading to "no rates" is the consumer's policy, not this layer's.
    only(FakeRates("primary", fail=ConnectionFailed("primary", "down")))
    with pytest.raises(ConnectionFailed):
        fx.rates(("USD", "GBP"))


def test_an_empty_chain_says_so():
    only()
    with pytest.raises(ConnectionFailed, match="no sources registered"):
        fx.rates(("USD", "GBP"))


def test_a_fresh_cache_costs_no_call():
    primary = FakeRates("primary", MARKET)
    only(primary)
    fx.rates(("USD", "GBP"))
    fx.rates(("USD", "GBP"))
    assert primary.calls == 1


def test_use_cache_false_refetches_and_still_refreshes_the_cache():
    primary = FakeRates("primary", MARKET)
    only(primary)
    fx.rates(("USD", "GBP"))
    fx.rates(("USD", "GBP"), use_cache=False)
    assert primary.calls == 2
    assert cache.read() is not None


def test_a_currency_outside_the_cache_refetches():
    primary = FakeRates("primary", MARKET)
    only(primary)
    fx.rates(("USD", "GBP"))
    fx.rates(("USD", "GBP", "JPY"))
    assert primary.calls == 2


def test_a_stale_cache_beats_nothing_and_says_so():
    only(FakeRates("primary", MARKET))
    fx.rates(("USD", "GBP"))
    _age_the_cache(hours=48)

    only(FakeRates("primary", fail=ConnectionFailed("primary", "down")))
    table = fx.rates(("USD", "GBP"))
    assert table.stale is True
    assert table.source == "primary"
    assert table.rate("GBP") == 0.73869


def test_a_stale_cache_that_misses_a_currency_is_not_used():
    only(FakeRates("primary", MARKET))
    fx.rates(("USD", "GBP"))
    _age_the_cache(hours=48)

    only(FakeRates("primary", fail=ConnectionFailed("primary", "down")))
    with pytest.raises(ConnectionFailed):
        fx.rates(("USD", "GBP", "CHF"))


def test_a_fresh_table_is_never_flagged_stale():
    only(FakeRates("primary", MARKET))
    assert fx.rates(("USD", "GBP")).stale is False


def test_the_pivot_is_always_requested_even_if_not_asked_for():
    primary = FakeRates("primary", MARKET)
    only(primary)
    assert fx.rates(("GBP",)).has("USD")


def test_a_pinned_source_skips_the_chain():
    primary, fallback = FakeRates("primary", MARKET), FakeRates("fallback", FIXING)
    only(primary, fallback)
    assert fx.rates(("USD", "GBP"), source="fallback").source == "fallback"
    assert primary.calls == 0


def test_a_pinned_source_is_not_answered_from_another_sources_cache():
    primary, fallback = FakeRates("primary", MARKET), FakeRates("fallback", FIXING)
    only(primary, fallback)
    fx.rates(("USD", "GBP"))  # caches primary's table
    table = fx.rates(("USD", "GBP"), source="fallback")
    assert table.source == "fallback"
    assert table.rate("GBP") == 0.73624


def test_an_unknown_pinned_source_is_reported_as_such():
    only(FakeRates("primary", MARKET))
    with pytest.raises(ConnectionFailed) as caught:
        fx.rates(("USD", "GBP"), source="nope")
    assert "unknown source" in str(caught.value)


def test_ttl_is_overridable():
    primary = FakeRates("primary", MARKET)
    only(primary)
    fx.rates(("USD", "GBP"))
    fx.rates(("USD", "GBP"), ttl=timedelta(0))
    assert primary.calls == 2


def test_clear_cache_forces_a_refetch():
    primary = FakeRates("primary", MARKET)
    only(primary)
    fx.rates(("USD", "GBP"))
    fx.clear_cache()
    fx.rates(("USD", "GBP"))
    assert primary.calls == 2


def test_source_lookup_and_registration():
    only(FakeRates("primary", MARKET))
    assert fx.source("primary").name == "primary"
    with pytest.raises(UnknownSource):
        fx.source("nope")


def test_register_first_puts_a_source_at_the_head_of_the_chain():
    fx.register("mine", FakeRates("mine", MARKET), first=True)
    assert fx.chain()[0] == "mine"


def test_replacing_a_source_keeps_its_place_in_the_chain():
    fx.register("frankfurter", FakeRates("frankfurter", FIXING))
    assert fx.chain() == ["yfinance", "frankfurter"]


def _age_the_cache(hours: float) -> None:
    raw = json.loads(cache.path().read_text())
    raw["fetched_at"] = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    cache.path().write_text(json.dumps(raw))
