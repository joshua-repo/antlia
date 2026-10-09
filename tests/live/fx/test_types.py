"""The canonical table: conversion, orientation, and refusing to guess."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from antlia.live.fx.types import PIVOT, RateTable

# The rates measured on 2026-08-28/29, so the arithmetic below is checkable
# against a real screen rather than against round numbers.
LIVE = {"USD": 1.0, "GBP": 0.73869, "JPY": 160.038, "HKD": 7.8391}


def table(rates=None, **kwargs):
    return RateTable(
        base=PIVOT,
        rates=rates or LIVE,
        as_of=kwargs.pop("as_of", datetime(2026, 8, 28, 16, 0, tzinfo=UTC)),
        source=kwargs.pop("source", "yfinance"),
        **kwargs,
    )


def test_convert_crosses_through_the_pivot():
    # The figure from the brief: a GBP account total restated in USD.
    assert round(table().convert(83289.64, "GBP", "USD"), 2) == 112753.17


def test_convert_between_two_non_pivot_currencies():
    got = table().convert(1000.0, "GBP", "JPY")
    assert round(got, 2) == round(1000.0 / 0.73869 * 160.038, 2)


def test_convert_is_identity_within_one_currency():
    # No rate is consulted, so this holds even for a currency not in the table.
    assert table().convert(12.5, "CHF", "CHF") == 12.5


def test_convert_never_invents_a_rate():
    assert table().convert(100.0, "CHF", "USD") is None
    assert table().convert(100.0, "USD", "CHF") is None


def test_convert_passes_a_missing_amount_through():
    assert table().convert(None, "GBP", "USD") is None


def test_a_zero_rate_is_refused_rather_than_dividing():
    assert table({"USD": 1.0, "XXX": 0.0}).convert(100.0, "XXX", "USD") is None


def test_inverse_is_the_orientation_brokers_report():
    # IBKR reports base-per-unit: a USD-based account carries 0.006249 for JPY,
    # not 160.038. Comparing across orientations is wrong by ~25,000x.
    assert round(table().inverse("JPY"), 6) == 0.006249
    assert table().inverse("CHF") is None


def test_rate_and_has():
    assert table().rate("GBP") == 0.73869
    assert table().rate("CHF") is None
    assert table().has("HKD") and not table().has("CHF")


def test_age_reads_a_naive_stamp_as_utc():
    naive = table(as_of=datetime.now(UTC).replace(tzinfo=None))
    assert naive.age < timedelta(seconds=5)


def test_str_says_which_source_and_whether_stale():
    assert "yfinance" in str(table())
    assert "(stale)" in str(table(stale=True))
