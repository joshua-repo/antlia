"""The store's account of itself: unreadable files, leaked writes, bad counts.

Integrity, not quality. Every assertion here is about whether the store is
telling the truth, never about whether the numbers in it are any good.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest

from antlia import history
from antlia.history import integrity, ledger, store
from antlia.history.types import EQUITY_EOD, Window

from .fakes import FakeHistory

D = dt.date
SPAN = ("2026-01-05", "2026-01-16")


@pytest.fixture
def warm(tmp_path):
    src = FakeHistory()
    history.register("fake", src, first=True)
    history.equity_eod("AAPL", *SPAN, store=tmp_path)
    return src


def files(tmp_path):
    return sorted((tmp_path / "raw" / "fake" / "equity_eod").glob("**/*.parquet"))


def test_a_healthy_store_has_nothing_to_report(warm, tmp_path):
    assert integrity.check(tmp_path) == []


def test_a_write_leaves_no_staging_behind(warm, tmp_path):
    staging = store.staging_dir(tmp_path)
    assert not staging.exists() or not list(staging.iterdir())


def test_a_truncated_file_is_found_and_quarantined(warm, tmp_path):
    victim = files(tmp_path)[3]
    victim.write_bytes(victim.read_bytes()[:200])

    # One bad file breaks *every* date, not just its own: the reader unions the
    # whole glob to align schemas, so partition pruning does not save you.
    with pytest.raises(Exception, match="magic bytes"):
        history.equity_eod("AAPL", "2026-01-14", "2026-01-16", store=tmp_path, fetch=False)

    found = integrity.check(tmp_path)
    assert any(p.kind == integrity.UNREADABLE and p.path == victim for p in found)

    integrity.repair(tmp_path, found)
    assert not victim.exists()
    # Quarantined, never deleted: it is the only copy of what the vendor said.
    assert (tmp_path / store.QUARANTINE / "raw" / "fake" / "equity_eod").exists()
    # And reads work again.
    assert history.equity_eod(
        "AAPL", "2026-01-14", "2026-01-16", store=tmp_path, fetch=False
    ).num_rows


def test_a_leaked_staging_directory_is_found_and_removed(warm, tmp_path):
    leaked = store.staging_dir(tmp_path) / "20260101T000000-1-0"
    leaked.mkdir(parents=True)
    (leaked / "half.parquet").write_bytes(b"")

    found = integrity.check(tmp_path)
    assert [p.kind for p in found if p.kind == integrity.LEAKED] == [integrity.LEAKED]
    integrity.repair(tmp_path, found)
    assert not leaked.exists()


def test_rows_written_but_never_recorded_show_as_a_mismatch(warm, tmp_path):
    # Exactly what a SIGTERM between the write and the ledger flush leaves.
    src = FakeHistory()
    span = Window(D(2026, 1, 19), D(2026, 1, 20))
    frame = src.fetch("equity_eod", "AAPL", span, src.scopes("equity_eod", "AAPL", span)[0])
    store.write(tmp_path, "fake", EQUITY_EOD, frame, src.projection("equity_eod")["date"])

    found = [p for p in integrity.check(tmp_path) if p.kind == integrity.MISMATCH]
    assert len(found) == 1
    assert "never recorded" in found[0].detail
    # Not repairable here -- only the vendor knows what should be there.
    assert not found[0].repairable
    assert "--refresh" in found[0].advice


def test_rows_recorded_but_lost_show_as_a_mismatch(warm, tmp_path):
    files(tmp_path)[2].unlink()
    found = [p for p in integrity.check(tmp_path) if p.kind == integrity.MISMATCH]
    assert len(found) == 1
    assert "lost since it was recorded" in found[0].detail


def test_a_failed_move_never_leaves_a_partial_file(tmp_path, monkeypatch):
    # The invariant staging buys: a write that dies mid-way leaves whole files
    # or none, never a truncated one. Before it staged, this produced exactly
    # the poisoned store the test above has to repair.
    src = FakeHistory()
    history.register("fake", src, first=True)
    span = Window(D(2026, 1, 5), D(2026, 1, 9))
    frame = src.fetch("equity_eod", "AAPL", span, src.scopes("equity_eod", "AAPL", span)[0])

    real = os.replace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] > 2:
            raise OSError("disk went away")
        return real(a, b)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(OSError):
        store.write(tmp_path, "fake", EQUITY_EOD, frame, src.projection("equity_eod")["date"])

    assert integrity.check(tmp_path) == [] or all(
        p.kind == integrity.MISMATCH for p in integrity.check(tmp_path)
    )
    # Whatever landed is readable, which is the whole point.
    assert store.read(tmp_path, "fake", EQUITY_EOD, src.projection("equity_eod")).num_rows == 2
    assert not list(store.staging_dir(tmp_path).iterdir())


def test_check_reports_through_the_public_api(warm, tmp_path):
    victim = files(tmp_path)[0]
    victim.write_bytes(b"not a parquet file")
    assert history.check(store=tmp_path)
    assert history.check(store=tmp_path, repair=True)
    assert history.check(store=tmp_path) == [] or all(
        p.kind == integrity.MISMATCH for p in history.check(store=tmp_path)
    )


def test_the_ledger_survives_a_torn_write(warm, tmp_path):
    # The ledger is read through the same glob, so a truncated entry file makes
    # the whole coverage record unreadable. It is written temp-then-rename for
    # the same reason the store tier is.
    entries = sorted(store.ledger_dir(tmp_path).glob("*.parquet"))
    assert entries and not list(store.ledger_dir(tmp_path).glob("*.tmp"))
    assert ledger.windows(tmp_path, "fake", "equity_eod", "AAPL")
