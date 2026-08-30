"""The Parquet tier: what a raw file contains, and what a read makes of it."""

from __future__ import annotations

import datetime as dt

import pyarrow.parquet as pq
import pytest

from antlia.history import store
from antlia.history.types import EQUITY_EOD, Window

from .fakes import FakeHistory

D = dt.date


def write(base, src, window, **kw):
    spec = EQUITY_EOD
    frame = src.fetch(spec.name, "AAPL", window, src.scopes(spec.name, "AAPL", window)[0])
    return store.write(base, src.name, spec, frame, src.projection(spec.name)["date"], **kw)


def read(base, src, **kw):
    return store.read(base, src.name, EQUITY_EOD, src.projection(EQUITY_EOD.name), **kw)


def test_an_empty_store_answers_with_the_right_shape(tmp_path):
    src = FakeHistory()
    out = read(tmp_path, src)
    assert out.num_rows == 0
    assert out.column_names == list(EQUITY_EOD.names)


def test_raw_files_hold_the_vendor_columns_plus_provenance(tmp_path):
    src = FakeHistory()
    write(tmp_path, src, Window(D(2026, 1, 5), D(2026, 1, 6)))
    files = sorted((tmp_path / "raw" / "fake" / "equity_eod").glob("**/*.parquet"))
    assert files, "nothing was written"
    columns = set(pq.read_table(files[0]).column_names)
    # Vendor spelling survives untouched -- `count`, not `trades`.
    assert {"created", "count", "bid_size", "symbol"} <= columns
    assert {"source", "ingested_at"} <= columns
    # The partition value lives in the path, not in the file.
    assert store.PARTITION not in columns
    assert files[0].parent.name.startswith(f"{store.PARTITION}=")


def test_the_read_returns_the_canonical_schema(tmp_path):
    src = FakeHistory()
    write(tmp_path, src, Window(D(2026, 1, 5), D(2026, 1, 9)))
    out = read(tmp_path, src)
    assert out.column_names == list(EQUITY_EOD.names)
    # `count` became `trades`; the vendor name is nowhere in the read.
    assert "count" not in out.column_names
    assert out.column("trades").to_pylist() == [3] * 5


def test_the_session_date_is_cast_in_new_york(tmp_path):
    # The stamp is 17:15 New York. A machine casting it in UTC lands on the
    # same day here, but the test pins the intent: the venue's calendar, not
    # the reader's locale.
    src = FakeHistory()
    write(tmp_path, src, Window(D(2026, 1, 5), D(2026, 1, 5)))
    assert read(tmp_path, src).column("date").to_pylist() == [D(2026, 1, 5)]


def test_a_restatement_wins_and_the_old_row_survives(tmp_path):
    old = FakeHistory(close=1.5)
    new = FakeHistory(close=9.9)
    span = Window(D(2026, 1, 5), D(2026, 1, 5))
    write(tmp_path, old, span, ingested_at=dt.datetime(2026, 1, 6, tzinfo=dt.UTC))
    write(tmp_path, new, span, ingested_at=dt.datetime(2026, 1, 7, tzinfo=dt.UTC))

    assert read(tmp_path, old).column("close").to_pylist() == [9.9]
    # Nothing was edited: the earlier append is still there to be asked for.
    every = read(tmp_path, old, latest=False)
    assert every.column("close").to_pylist() == [1.5, 9.9]


def test_the_date_filter_narrows_before_the_scan(tmp_path):
    src = FakeHistory()
    write(tmp_path, src, Window(D(2026, 1, 5), D(2026, 1, 16)))
    out = read(tmp_path, src, start=D(2026, 1, 12), end=D(2026, 1, 14))
    assert out.column("date").to_pylist() == [D(2026, 1, 12), D(2026, 1, 13), D(2026, 1, 14)]


def test_a_projection_missing_a_column_fails_loudly(tmp_path):
    src = FakeHistory()
    write(tmp_path, src, Window(D(2026, 1, 5), D(2026, 1, 5)))
    partial = {k: v for k, v in src.projection("equity_eod").items() if k != "bid"}
    with pytest.raises(KeyError, match="bid"):
        store.read(tmp_path, src.name, EQUITY_EOD, partial)


def test_a_dated_table_needs_a_date_expression(tmp_path):
    src = FakeHistory()
    span = Window(D(2026, 1, 5), D(2026, 1, 5))
    frame = src.fetch("equity_eod", "AAPL", span, src.scopes("equity_eod", "AAPL", span)[0])
    with pytest.raises(ValueError, match="partitioned by date"):
        store.write(tmp_path, src.name, EQUITY_EOD, frame, None)


def test_the_root_prefers_the_argument_then_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(store.ENV_ROOT, str(tmp_path / "env"))
    assert store.root() == tmp_path / "env"
    assert store.root(tmp_path / "explicit") == tmp_path / "explicit"
    monkeypatch.delenv(store.ENV_ROOT)
    assert store.root().name == "store"
