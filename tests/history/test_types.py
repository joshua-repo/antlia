"""Window arithmetic. Every saved request in the layer comes out of this file."""

from __future__ import annotations

import datetime as dt

import pytest

from antlia.history.types import (
    OPTION_EOD,
    Window,
    as_date,
    merge,
    subtract,
    table,
    window,
)

D = dt.date


def test_a_window_includes_both_ends():
    span = window("2026-01-01", "2026-01-03")
    assert span.days == 3
    assert D(2026, 1, 1) in span and D(2026, 1, 3) in span
    assert D(2026, 1, 4) not in span


def test_a_backwards_window_is_refused_at_construction():
    with pytest.raises(ValueError, match="ends before it starts"):
        window("2026-01-03", "2026-01-01")


def test_chunks_respect_an_inclusive_cap():
    # ThetaData rejects a 366-day span outright rather than trimming it, so a
    # chunk one day too wide costs the whole request.
    pieces = window("2026-01-01", "2026-01-25").chunks(10)
    assert [str(p) for p in pieces] == [
        "2026-01-01 .. 2026-01-10",
        "2026-01-11 .. 2026-01-20",
        "2026-01-21 .. 2026-01-25",
    ]
    assert all(p.days <= 10 for p in pieces)


def test_merge_joins_adjacent_windows():
    # A gap of zero days is not a gap: leaving these apart would make the
    # planner re-request the seam between two earlier runs.
    joined = merge([window("2026-01-01", "2026-01-03"), window("2026-01-04", "2026-01-06")])
    assert [str(w) for w in joined] == ["2026-01-01 .. 2026-01-06"]


def test_merge_collapses_overlaps_and_sorts():
    joined = merge(
        [
            window("2026-01-10", "2026-01-20"),
            window("2026-01-01", "2026-01-05"),
            window("2026-01-15", "2026-01-25"),
        ]
    )
    assert [str(w) for w in joined] == ["2026-01-01 .. 2026-01-05", "2026-01-10 .. 2026-01-25"]


def test_subtract_leaves_the_holes():
    gaps = subtract(window("2026-01-01", "2026-01-31"), [window("2026-01-10", "2026-01-20")])
    assert [str(g) for g in gaps] == ["2026-01-01 .. 2026-01-09", "2026-01-21 .. 2026-01-31"]


def test_subtract_of_a_full_cover_is_nothing():
    assert subtract(window("2026-01-05", "2026-01-10"), [window("2026-01-01", "2026-01-31")]) == []


def test_subtract_ignores_windows_that_do_not_touch():
    whole = window("2026-01-01", "2026-01-05")
    assert subtract(whole, [window("2026-02-01", "2026-02-05")]) == [whole]


def test_clamp_returns_none_when_nothing_survives():
    assert window("2026-01-01", "2026-01-05").clamp(start=D(2026, 6, 1)) is None


def test_as_date_takes_what_a_caller_had_to_hand():
    assert as_date("2026-01-02") == D(2026, 1, 2)
    assert as_date(D(2026, 1, 2)) == D(2026, 1, 2)
    assert as_date(dt.datetime(2026, 1, 2, 17, 15)) == D(2026, 1, 2)


def test_the_option_key_is_the_occ_identity():
    # Open question 1 is answered for history by this tuple and no other: it
    # needs no cross-source id map to exist first.
    assert OPTION_EOD.key == ("date", "symbol", "expiration", "strike", "right")


def test_every_table_carries_provenance():
    for name in ("equity_eod", "option_eod", "expirations"):
        assert table(name).names[-2:] == ("source", "ingested_at")


def test_an_unknown_table_names_the_known_ones():
    with pytest.raises(KeyError, match="equity_eod"):
        table("futures_eod")


def test_a_window_of_one_day_chunks_to_itself():
    assert [str(c) for c in Window(D(2026, 1, 1), D(2026, 1, 1)).chunks(365)] == [
        "2026-01-01 .. 2026-01-01"
    ]
