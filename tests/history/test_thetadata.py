"""The ThetaData adapter, offline: mapping, planning, and error classification.

The vendor facts these pin were measured against a live key on 2026-08-30 and
are recorded in the adapter's docstring. What is tested here is that the code
acts on them -- a network test would prove the vendor still behaves that way,
which is what `antlia-history verify` is for.
"""

from __future__ import annotations

import datetime as dt

import pyarrow as pa
import pytest

from antlia.history import store
from antlia.history.errors import IngestFailed, NotEntitled
from antlia.history.sources.thetadata import (
    MAX_SPAN_DAYS,
    MAX_WORKERS,
    ThetaDataHistory,
)
from antlia.history.types import OPTION_EOD, Window

D = dt.date
NY = "America/New_York"


class Denied(Exception):
    """Shaped like the grpc error the SDK lets through: `.code()` and `.details()`."""

    def code(self):
        return "StatusCode.PERMISSION_DENIED"

    def details(self):
        return "Requesting an option endpoint requiring a value subscription"


class Broken(Exception):
    def code(self):
        return "StatusCode.UNAVAILABLE"

    def details(self):
        return "socket closed"


class NoDataFoundError(Exception):
    """Same name as the SDK's, which is what the adapter classifies on."""


def vendor_option_frame():
    """One expiration over two sessions, exactly as the SDK hands it over."""
    return pa.table(
        {
            "symbol": ["AAPL"] * 2,
            "expiration": ["2026-09-18"] * 2,
            "strike": [310.0, 310.0],
            "right": ["CALL", "PUT"],
            "created": pa.array(
                [dt.datetime(2026, 8, 26, 17, 15)] * 2, type=pa.timestamp("ms", tz=NY)
            ),
            "last_trade": pa.array(
                [dt.datetime(2026, 8, 26, 16, 0)] * 2, type=pa.timestamp("ms", tz=NY)
            ),
            "open": [5.0, 4.0],
            "high": [6.0, 4.5],
            "low": [4.5, 3.5],
            "close": [5.5, 3.8],
            "volume": [100, 200],
            "count": [10, 20],
            "bid_size": [1, 2],
            "bid_exchange": [7, 7],
            "bid": [5.4, 3.7],
            "bid_condition": [50, 50],
            "ask_size": [3, 4],
            "ask_exchange": [65, 65],
            "ask": [5.6, 3.9],
            "ask_condition": [50, 50],
        }
    )


def test_the_span_cap_matches_the_measured_limit():
    # 365 works and 366 is INVALID_ARGUMENT, measured. One day over fails the
    # whole request rather than being trimmed.
    assert ThetaDataHistory().max_span_days == MAX_SPAN_DAYS == 365


def test_the_projection_normalises_the_vendor_frame(tmp_path):
    src = ThetaDataHistory()
    written = store.write(
        tmp_path, src.name, OPTION_EOD, vendor_option_frame(), src.projection("option_eod")["date"]
    )
    assert written == 2

    out = store.read(tmp_path, src.name, OPTION_EOD, src.projection("option_eod"))
    rows = out.to_pydict()
    assert rows["date"] == [D(2026, 8, 26)] * 2
    assert rows["expiration"] == [D(2026, 9, 18)] * 2
    # CALL/PUT become the one-letter canonical rights, sorted by the key.
    assert rows["right"] == ["C", "P"]
    assert rows["trades"] == [10, 20]
    assert rows["strike"] == [310.0, 310.0]
    # The vendor's own spellings never reach a consumer.
    assert "count" not in out.column_names
    assert "bid_condition" not in out.column_names


def test_the_raw_file_keeps_every_vendor_column(tmp_path):
    import pyarrow.parquet as pq

    src = ThetaDataHistory()
    store.write(
        tmp_path, src.name, OPTION_EOD, vendor_option_frame(), src.projection("option_eod")["date"]
    )
    files = sorted((tmp_path / "raw" / "thetadata" / "option_eod").glob("**/*.parquet"))
    columns = set(pq.read_table(files[0]).column_names)
    # Including the ones the canonical schema drops: raw is what can never be
    # regenerated, so nothing is thrown away on the way in.
    assert {"bid_exchange", "ask_condition", "last_trade", "count"} <= columns


def test_the_session_date_is_cast_in_new_york(tmp_path):
    # `created` is 17:15 New York. Casting in the reader's own zone is how a
    # session lands on the wrong day, and the projection pins the venue.
    assert NY in ThetaDataHistory().projection("equity_eod")["date"]


def test_a_denial_is_classified_as_permanent():
    src = ThetaDataHistory()
    with pytest.raises(NotEntitled, match="value subscription"):
        src._call(_raiser(Denied()))


def test_no_data_found_is_not_a_failure():
    # A market holiday raises rather than returning an empty frame. Treating
    # that as an error would abort an ingest run on every holiday in the range.
    assert ThetaDataHistory()._call(_raiser(NoDataFoundError("no data"))) is None


def test_anything_else_is_transient():
    src = ThetaDataHistory()
    with pytest.raises(IngestFailed, match="socket closed"):
        src._call(_raiser(Broken()))


def test_the_horizon_comes_from_the_environment_when_set(monkeypatch):
    monkeypatch.setenv("ANTLIA_THETADATA_EARLIEST", "2024-02-01")
    assert ThetaDataHistory().earliest("equity_eod") == D(2024, 2, 1)


def test_an_explicit_horizon_beats_the_environment(monkeypatch):
    monkeypatch.setenv("ANTLIA_THETADATA_EARLIEST", "2024-02-01")
    assert ThetaDataHistory(earliest=D(2025, 1, 1)).earliest("equity_eod") == D(2025, 1, 1)


def test_scopes_drop_expirations_that_cannot_appear_in_the_window():
    src = ThetaDataHistory()
    listed = [D(2026, 1, 16), D(2026, 3, 20), D(2028, 12, 15)]
    window = Window(D(2026, 2, 1), D(2026, 2, 28))
    keys = [s.key for s in src.scopes("option_eod", "AAPL", window, expirations=listed)]
    # January expired before the window opened; the LEAP is beyond max_dte.
    assert keys == ["2026-03-20", "2028-12-15"]
    capped = src.scopes("option_eod", "AAPL", window, expirations=listed, max_dte=45)
    assert [s.key for s in capped] == ["2026-03-20"]


def test_a_scope_is_bounded_by_its_own_expiry():
    src = ThetaDataHistory()
    scope = src.scopes(
        "option_eod",
        "AAPL",
        Window(D(2026, 1, 1), D(2026, 12, 31)),
        expirations=[D(2026, 3, 20)],
    )[0]
    assert str(scope.clamp(Window(D(2026, 1, 1), D(2026, 12, 31)))) == "2026-01-01 .. 2026-03-20"


def test_non_eod_tables_are_not_claimed():
    # The FREE plan serves EOD and nothing else; a table this source cannot
    # fill is better absent than half-populated.
    assert ThetaDataHistory().tables == frozenset({"equity_eod", "option_eod", "expirations"})


def _raiser(exc):
    def call(client):
        raise exc

    return call


@pytest.fixture(autouse=True)
def _no_session(monkeypatch):
    """`_call` opens an auth session; the tests here are about what happens inside it."""
    import contextlib

    from antlia import auth

    @contextlib.contextmanager
    def session(*args, **kwargs):
        yield object()

    monkeypatch.setattr(auth, "session", session)
    monkeypatch.setattr(auth, "limiter", lambda *a, **k: _Unlimited())


class _Unlimited:
    def acquire(self, *args, **kwargs):
        return True


def test_the_concurrency_ceiling_matches_the_measured_limit():
    # Measured 2026-08-30: --workers 2 served six requests in 8.2s, --workers 3
    # was refused outright.
    assert ThetaDataHistory().max_workers == MAX_WORKERS == 2


def test_a_concurrency_refusal_names_the_flag_to_change():
    class TooMany(Exception):
        def code(self):
            return "StatusCode.RESOURCE_EXHAUSTED"

        def details(self):
            return "Too many concurrent requests. Please reduce the number of parallel requests."

    src = ThetaDataHistory()
    with pytest.raises(IngestFailed, match="--workers"):
        src._call(_raiser(TooMany()))
