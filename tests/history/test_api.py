"""The read surface: cache-first, one schema, and `fetch=` meaning both things."""

from __future__ import annotations

import datetime as dt

import pytest

from antlia import history
from antlia.history.errors import NotCovered

from .fakes import EXPIRATIONS, FakeHistory

D = dt.date
JAN = ("2026-01-05", "2026-01-16")


@pytest.fixture
def fake(tmp_path):
    src = FakeHistory()
    history.register("fake", src, first=True)
    return src


def test_the_chain_puts_a_registered_source_first(fake):
    assert history.chain()[0] == "fake"
    assert history.source().name == "fake"


def test_a_read_fetches_what_is_missing_by_default(fake, tmp_path):
    out = history.equity_eod("AAPL", *JAN, store=tmp_path)
    assert out.num_rows == 10
    assert out.column_names == list(history.types.EQUITY_EOD.names)


def test_a_covered_read_makes_no_vendor_call(fake, tmp_path):
    history.equity_eod("AAPL", *JAN, store=tmp_path)
    fake.calls.clear()
    assert history.equity_eod("AAPL", *JAN, store=tmp_path).num_rows == 10
    assert fake.calls == []


def test_fetch_false_refuses_and_names_the_gaps(fake, tmp_path):
    with pytest.raises(NotCovered) as raised:
        history.equity_eod("AAPL", *JAN, store=tmp_path, fetch=False)
    assert "2026-01-05 .. 2026-01-16" in str(raised.value)
    assert raised.value.missing
    assert fake.calls == []


def test_the_environment_flips_the_default(fake, tmp_path, monkeypatch):
    monkeypatch.setenv(history.ENV_FETCH, "0")
    with pytest.raises(NotCovered):
        history.equity_eod("AAPL", *JAN, store=tmp_path)


def test_frames_convert_on_request(fake, tmp_path):
    history.equity_eod("AAPL", *JAN, store=tmp_path)
    frame = history.equity_eod("AAPL", *JAN, store=tmp_path, frame="pandas")
    assert list(frame.columns) == list(history.types.EQUITY_EOD.names)
    assert history.equity_eod("AAPL", *JAN, store=tmp_path, frame="polars").height == 10


def test_an_unknown_frame_is_refused(fake, tmp_path):
    history.equity_eod("AAPL", *JAN, store=tmp_path)
    with pytest.raises(ValueError, match="unknown frame"):
        # Typed as a Literal, so this is what a caller reaching past the hint
        # gets -- a named refusal, not a confusing arrow error further down.
        history.equity_eod("AAPL", *JAN, store=tmp_path, frame="csv")  # type: ignore[arg-type]


def test_expirations_are_cached_so_a_covered_read_never_authenticates(fake, tmp_path):
    assert history.expirations("AAPL", store=tmp_path) == list(EXPIRATIONS)
    fake.calls.clear()
    assert history.expirations("AAPL", store=tmp_path) == list(EXPIRATIONS)
    assert fake.calls == []


def test_option_rows_carry_the_canonical_right(fake, tmp_path):
    out = history.option_eod("AAPL", "2026-01-05", "2026-01-09", store=tmp_path)
    assert set(out.column("right").to_pylist()) == {"C"}


def test_row_filters_cost_nothing_and_narrow_the_result(fake, tmp_path):
    history.option_eod("AAPL", "2026-01-05", "2026-01-09", store=tmp_path)
    fake.calls.clear()
    one = history.option_eod(
        "AAPL", "2026-01-05", "2026-01-09", expiration="2026-01-16", store=tmp_path
    )
    assert set(one.column("expiration").to_pylist()) == {D(2026, 1, 16)}
    assert (
        history.option_eod("AAPL", "2026-01-05", "2026-01-09", right="put", store=tmp_path).num_rows
        == 0
    )
    assert (
        history.option_eod(
            "AAPL", "2026-01-05", "2026-01-09", strikes=(200.0, 300.0), store=tmp_path
        ).num_rows
        == 0
    )


def test_an_unknown_right_is_refused_before_a_query_runs(fake, tmp_path):
    with pytest.raises(ValueError, match="unknown right"):
        history.option_eod("AAPL", *JAN, right="X", store=tmp_path)


def test_dte_filters_rows_and_shapes_the_plan(fake, tmp_path):
    out = history.option_eod("AAPL", "2026-01-05", "2026-01-09", dte=(0, 15), store=tmp_path)
    # Only the January expiration is within 15 days, so February is never
    # fetched -- the filter shaped the plan, not just the result.
    assert {key for _, _, _, key in fake.calls if key} == {"2026-01-16"}
    assert set(out.column("expiration").to_pylist()) == {D(2026, 1, 16)}


def test_coverage_separates_missing_from_denied(tmp_path):
    src = FakeHistory(earliest=D(2026, 1, 10))
    history.register("fake", src, first=True)
    history.equity_eod("AAPL", "2026-01-12", "2026-01-16", store=tmp_path)

    covered = history.coverage("equity_eod", "AAPL", "2026-01-12", "2026-01-16", store=tmp_path)
    assert covered.complete and covered.rows == 5

    wider = history.coverage("equity_eod", "AAPL", "2026-01-05", "2026-01-20", store=tmp_path)
    assert [str(w) for w in wider.denied] == ["2026-01-05 .. 2026-01-09"]
    assert [str(w) for w in wider.missing] == [
        "2026-01-10 .. 2026-01-11",
        "2026-01-17 .. 2026-01-20",
    ]
    assert not wider.complete


def test_plan_costs_nothing(fake, tmp_path):
    plan = history.plan("equity_eod", "AAPL", *JAN, store=tmp_path)
    assert plan.calls == 2
    assert fake.calls == []


def test_symbols_lists_what_has_been_asked_about(fake, tmp_path):
    history.equity_eod("AAPL", *JAN, store=tmp_path)
    assert history.symbols("equity_eod", store=tmp_path) == ["AAPL"]


def test_a_covered_window_is_never_refetched_without_asking(fake, tmp_path):
    history.equity_eod("AAPL", "2026-01-05", "2026-01-06", store=tmp_path)
    history.register("fake", FakeHistory(close=9.9), first=True)
    report = history.fill("equity_eod", "AAPL", "2026-01-05", "2026-01-06", store=tmp_path)
    assert report.calls == 0


def test_refresh_appends_a_restatement_and_latest_picks_it(fake, tmp_path):
    span = ("2026-01-05", "2026-01-06")
    history.equity_eod("AAPL", *span, store=tmp_path)
    history.register("fake", FakeHistory(close=9.9), first=True)
    # Without `refresh` the ledger covers this and nothing happens -- which is
    # what made the whole restatement design unreachable before it existed.
    history.fill("equity_eod", "AAPL", *span, store=tmp_path, refresh=True)

    newest = history.equity_eod("AAPL", *span, store=tmp_path, fetch=False)
    assert newest.column("close").to_pylist() == [9.9, 9.9]

    every = history.equity_eod("AAPL", *span, store=tmp_path, fetch=False, latest=False)
    # Two sessions, two versions each. Nothing was edited.
    assert sorted(every.column("close").to_pylist()) == [1.5, 1.5, 9.9, 9.9]


def test_as_of_reads_what_the_source_said_at_a_past_moment(fake, tmp_path):
    span = ("2026-01-05", "2026-01-06")
    history.equity_eod("AAPL", *span, store=tmp_path)
    first = history.equity_eod("AAPL", *span, store=tmp_path, fetch=False)
    cutoff = max(first.column("ingested_at").to_pylist())

    history.register("fake", FakeHistory(close=9.9), first=True)
    history.fill("equity_eod", "AAPL", *span, store=tmp_path, refresh=True)

    assert history.equity_eod("AAPL", *span, store=tmp_path, fetch=False).column(
        "close"
    ).to_pylist() == [9.9, 9.9]
    # The study that ran before the restatement still reproduces exactly.
    back_then = history.equity_eod("AAPL", *span, store=tmp_path, fetch=False, as_of=cutoff)
    assert back_then.column("close").to_pylist() == [1.5, 1.5]


def test_as_of_before_anything_was_ingested_is_empty(fake, tmp_path):
    history.equity_eod("AAPL", "2026-01-05", "2026-01-06", store=tmp_path)
    old = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    assert (
        history.equity_eod(
            "AAPL", "2026-01-05", "2026-01-06", store=tmp_path, fetch=False, as_of=old
        ).num_rows
        == 0
    )


def test_a_read_takes_a_universe_in_one_frame(fake, tmp_path):
    out = history.equity_eod(["AAPL", "MSFT"], "2026-01-05", "2026-01-09", store=tmp_path)
    assert sorted(set(out.column("symbol").to_pylist())) == ["AAPL", "MSFT"]
    assert out.num_rows == 10


def test_a_universe_read_names_the_first_uncovered_symbol(fake, tmp_path):
    history.equity_eod("AAPL", "2026-01-05", "2026-01-09", store=tmp_path)
    with pytest.raises(NotCovered, match="MSFT"):
        history.equity_eod(
            ["AAPL", "MSFT"], "2026-01-05", "2026-01-09", store=tmp_path, fetch=False
        )


def test_an_empty_universe_is_refused(fake, tmp_path):
    with pytest.raises(ValueError, match="no symbol"):
        history.equity_eod([], "2026-01-05", "2026-01-09", store=tmp_path)


def test_the_store_root_follows_the_environment(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTLIA_STORE", str(tmp_path / "elsewhere"))
    history.equity_eod("AAPL", *JAN)
    assert (tmp_path / "elsewhere" / "raw" / "fake" / "equity_eod").exists()


def test_a_covered_option_read_never_asks_which_expirations_exist(fake, tmp_path):
    # The bug this pins: planning an option fetch needs the expiration list,
    # and asking the vendor for it made a fully covered read authenticate --
    # a request per backtest run, for an answer already on disk.
    history.option_eod("AAPL", "2026-01-05", "2026-01-09", store=tmp_path)
    fake.calls.clear()
    assert history.option_eod("AAPL", "2026-01-05", "2026-01-09", store=tmp_path).num_rows
    assert fake.calls == []


def test_filling_options_warms_the_listing_too(fake, tmp_path):
    history.fill("option_eod", "AAPL", "2026-01-05", "2026-01-09", store=tmp_path)
    fake.calls.clear()
    # Offline from here: the listing is in the store, so the plan is local.
    assert history.expirations("AAPL", store=tmp_path, fetch=False) == list(EXPIRATIONS)
    assert fake.calls == []


def test_an_option_read_without_a_cached_listing_refuses_offline(fake, tmp_path):
    with pytest.raises(NotCovered, match="expirations"):
        history.option_eod("AAPL", "2026-01-05", "2026-01-09", store=tmp_path, fetch=False)
    assert fake.calls == []


def test_an_unpublished_session_is_missing_not_complete(tmp_path):
    # Before the close, a window ending today must not read as complete: the
    # planner will not ask for today yet, and that is not the same as holding it.
    src = FakeHistory(published=D(2026, 1, 14))
    history.register("fake", src, first=True)
    history.equity_eod("AAPL", *JAN, store=tmp_path)
    covered = history.coverage("equity_eod", "AAPL", *JAN, store=tmp_path)
    assert [str(w) for w in covered.missing] == ["2026-01-15 .. 2026-01-16"]
    assert not covered.complete


def test_coverage_answers_from_the_store_alone(fake, tmp_path):
    # A question about what is held must not need the vendor. It did, through
    # the option planner's expiration listing, and that made `coverage()`
    # unusable on a machine with a warmed store and no SDK installed.
    history.option_eod("AAPL", "2026-01-05", "2026-01-09", store=tmp_path)
    fake.calls.clear()
    out = history.coverage("option_eod", "AAPL", "2026-01-05", "2026-01-09", store=tmp_path)
    assert out.complete
    assert fake.calls == []


def test_coverage_of_an_unknown_symbol_is_missing_not_complete(fake, tmp_path):
    out = history.coverage("option_eod", "ZZZZ", "2026-01-05", "2026-01-09", store=tmp_path)
    assert [str(w) for w in out.missing] == ["2026-01-05 .. 2026-01-09"]
    assert not out.complete
    assert fake.calls == []
