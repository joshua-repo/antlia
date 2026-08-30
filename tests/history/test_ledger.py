"""The coverage ledger: the difference between "nothing there" and "never asked"."""

from __future__ import annotations

import datetime as dt

from antlia.history import ledger
from antlia.history.types import Window

D = dt.date


def entry(status, start, end, scope="", rows=0, symbol="AAPL"):
    return ledger.Entry("fake", "equity_eod", symbol, scope, Window(start, end), rows, status)


def test_nothing_recorded_covers_nothing(tmp_path):
    assert ledger.windows(tmp_path, "fake", "equity_eod", "AAPL") == []


def test_an_empty_answer_still_counts_as_covered(tmp_path):
    # A market holiday leaves no rows. Without this the planner re-fetches
    # every holiday in the range on every run, forever.
    ledger.record(tmp_path, [entry(ledger.EMPTY, D(2026, 7, 3), D(2026, 7, 3))])
    assert [str(w) for w in ledger.windows(tmp_path, "fake", "equity_eod", "AAPL")] == [
        "2026-07-03 .. 2026-07-03"
    ]


def test_a_denial_is_neither_covered_nor_forgotten(tmp_path):
    ledger.record(tmp_path, [entry(ledger.DENIED, D(2020, 1, 1), D(2020, 1, 5))])
    assert ledger.windows(tmp_path, "fake", "equity_eod", "AAPL") == []
    assert [str(w) for w in ledger.denied(tmp_path, "fake", "equity_eod", "AAPL")] == [
        "2020-01-01 .. 2020-01-05"
    ]


def test_adjacent_runs_merge_into_one_window(tmp_path):
    ledger.record(tmp_path, [entry(ledger.OK, D(2026, 1, 1), D(2026, 1, 10), rows=5)])
    ledger.record(tmp_path, [entry(ledger.OK, D(2026, 1, 11), D(2026, 1, 20), rows=5)])
    assert [str(w) for w in ledger.windows(tmp_path, "fake", "equity_eod", "AAPL")] == [
        "2026-01-01 .. 2026-01-20"
    ]


def test_scopes_are_kept_apart(tmp_path):
    ledger.record(
        tmp_path,
        [
            entry(ledger.OK, D(2026, 1, 1), D(2026, 1, 5), scope="2026-01-16", rows=2),
            entry(ledger.OK, D(2026, 2, 1), D(2026, 2, 5), scope="2026-02-20", rows=2),
        ],
    )
    one = ledger.windows(tmp_path, "fake", "equity_eod", "AAPL", "2026-01-16")
    assert [str(w) for w in one] == ["2026-01-01 .. 2026-01-05"]
    # Asking without a scope is the symbol-level view, which is every scope.
    assert len(ledger.windows(tmp_path, "fake", "equity_eod", "AAPL")) == 2


def test_symbols_and_rows_are_reported_per_table(tmp_path):
    ledger.record(
        tmp_path,
        [
            entry(ledger.OK, D(2026, 1, 1), D(2026, 1, 5), rows=7),
            entry(ledger.OK, D(2026, 1, 1), D(2026, 1, 5), rows=3, symbol="MSFT"),
        ],
    )
    assert ledger.symbols(tmp_path, "fake", "equity_eod") == ["AAPL", "MSFT"]
    assert ledger.rows_for(tmp_path, "fake", "equity_eod", "AAPL") == 7


def test_recording_nothing_writes_nothing(tmp_path):
    assert ledger.record(tmp_path, []) == 0
    assert not (tmp_path / "ledger").exists()
