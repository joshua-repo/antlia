"""The expiration listing's age, and what coverage may claim because of it.

The defect this file pins produced a *wrong answer*, not a missing feature:
`coverage()` reported an option window `complete` while a whole expiration had
never been fetched, because the listing it planned from predated that
expiration being listed.
"""

from __future__ import annotations

import datetime as dt

import pytest

from antlia import history
from antlia.history import integrity, ledger
from antlia.history.types import Coverage, Window

from .fakes import FakeHistory

D = dt.date
WINDOW = ("2026-01-05", "2026-01-09")


@pytest.fixture
def warm(tmp_path):
    history.register("fake", FakeHistory(), first=True)
    history.option_eod("AAPL", *WINDOW, store=tmp_path)
    return tmp_path


def relist(base, when, expirations):
    """Rewrite the ledger so the listing was fetched on `when`, and list more.

    The store keeps the two expirations the fixture warmed; the source now
    lists a third. That is the shape of the bug: data planned from a listing
    that did not yet know about an expiration.
    """
    entries = [ledger.Entry("fake", "expirations", "AAPL", "", Window(when, when), 2, ledger.OK)]
    for key in ("2026-01-16", "2026-02-20"):
        entries.append(
            ledger.Entry(
                "fake",
                "option_eod",
                "AAPL",
                key,
                Window(D(2026, 1, 5), D(2026, 1, 9)),
                5,
                ledger.OK,
            )
        )
    for f in (base / "ledger").glob("*.parquet"):
        f.unlink()
    ledger.record(base, entries)
    history.register("fake", FakeHistory(expirations=expirations), first=True)


def test_a_listing_fetched_after_the_window_vouches_for_it(warm):
    relist(warm, D(2026, 3, 1), (D(2026, 1, 16), D(2026, 1, 23), D(2026, 2, 20)))
    out = history.coverage("option_eod", "AAPL", *WINDOW, store=warm)
    # An expiration is always listed before it trades, so a listing from March
    # already knows every expiration that traded in January.
    assert not out.stale_listing
    assert out.complete


def test_a_listing_fetched_before_the_window_ended_cannot(warm):
    relist(warm, D(2026, 1, 3), (D(2026, 1, 16), D(2026, 1, 23), D(2026, 2, 20)))
    out = history.coverage("option_eod", "AAPL", *WINDOW, store=warm)
    assert out.stale_listing
    # Not complete, even though `missing` is empty -- because `missing` was
    # computed from the same stale listing and cannot contain what it never
    # knew about.
    assert not out.missing
    assert not out.complete
    assert "expiration listing" in str(out)


def test_a_listing_never_fetched_cannot_vouch_either(tmp_path):
    out = Coverage(
        table="option_eod",
        symbol="AAPL",
        source="fake",
        requested=Window(D(2026, 1, 5), D(2026, 1, 9)),
        held=[],
        missing=[],
        denied=[],
        rows=0,
        listed_at=None,
    )
    assert out.stale_listing and not out.complete
    assert "never fetched" in str(out)


def test_the_rule_does_not_apply_to_equities(warm):
    # `equity_eod` has no listing to be stale, and must not inherit the doubt.
    history.equity_eod("AAPL", *WINDOW, store=warm)
    out = history.coverage("equity_eod", "AAPL", *WINDOW, store=warm)
    assert out.listed_at is None and not out.stale_listing and out.complete


def test_coverage_with_no_window_makes_no_claim(warm):
    out = history.coverage("option_eod", "AAPL", store=warm)
    assert out.requested is None and not out.stale_listing


def test_a_fill_refreshes_the_listing_and_finds_the_new_expiration(warm):
    relist(warm, D(2026, 1, 3), (D(2026, 1, 16), D(2026, 1, 23), D(2026, 2, 20)))
    assert not history.coverage("option_eod", "AAPL", *WINDOW, store=warm).complete
    report = history.fill("option_eod", "AAPL", *WINDOW, store=warm)
    assert report.calls == 1  # the expiration the stale listing hid
    assert history.coverage("option_eod", "AAPL", *WINDOW, store=warm).complete


def test_check_finds_data_newer_than_the_listing(warm):
    relist(warm, D(2026, 1, 3), (D(2026, 1, 16), D(2026, 2, 20)))
    stale = [p for p in integrity.check(warm) if p.kind == integrity.STALE]
    assert len(stale) == 1
    assert "expiration listing" in stale[0].detail
    # A vendor call is needed to fix it, and `check` does not make those.
    assert not stale[0].repairable


def test_check_is_quiet_when_the_listing_is_current(warm):
    assert [p for p in integrity.check(warm) if p.kind == integrity.STALE] == []


def test_ingests_lists_what_as_of_accepts(warm):
    stamps = history.ingests("option_eod", "AAPL", store=warm)
    assert stamps and all(s.tzinfo is not None for s in stamps)
    assert stamps == sorted(stamps)
    # And `as_of` the last of them is the same as the default read.
    latest = history.option_eod("AAPL", *WINDOW, store=warm, fetch=False)
    pinned = history.option_eod("AAPL", *WINDOW, store=warm, fetch=False, as_of=stamps[-1])
    assert latest.num_rows == pinned.num_rows


def test_ingests_grows_by_one_per_write(warm):
    before = len(history.ingests("equity_eod", "AAPL", store=warm))
    history.equity_eod("AAPL", *WINDOW, store=warm)
    after = len(history.ingests("equity_eod", "AAPL", store=warm))
    assert after == before + 1
    history.fill("equity_eod", "AAPL", *WINDOW, store=warm, refresh=True)
    assert len(history.ingests("equity_eod", "AAPL", store=warm)) == after + 1


def test_ingests_of_an_empty_store_is_empty(tmp_path):
    history.register("fake", FakeHistory(), first=True)
    assert history.ingests("equity_eod", "AAPL", store=tmp_path) == []
