"""FRED: rate series through the one write path, with no network."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import urllib.error
from email.message import Message
from zoneinfo import ZoneInfo

import pyarrow.dataset as ds
import pytest

from antlia import auth, history
from antlia.auth.errors import ConnectionFailed
from antlia.auth.providers.fred import FredHandle
from antlia.history import registry, store
from antlia.history.errors import IngestFailed
from antlia.history.sources.fred import FredHistory, published_through
from antlia.history.types import RATE_DAILY

D = dt.date
NY = ZoneInfo("America/New_York")


class FakeFred:
    """`session("fred")`'s handle, answering from canned observations."""

    def __init__(self, observations):
        self.observations = observations
        self.calls = []

    def get(self, endpoint, **params):
        self.calls.append((endpoint, params))
        lo, hi = params["observation_start"], params["observation_end"]
        return {"observations": [o for o in self.observations if lo <= o["date"] <= hi]}


def obs(date, value):
    vintage = {"realtime_start": "2026-10-07", "realtime_end": "2026-10-07"}
    return {**vintage, "date": date, "value": value}


@pytest.fixture
def fred(monkeypatch):
    handle = FakeFred(
        [obs("2026-01-05", "4.22"), obs("2026-01-06", "."), obs("2026-01-07", "4.19")]
    )

    @contextlib.contextmanager
    def session(*args, **kwargs):
        yield handle

    monkeypatch.setattr(auth, "session", session)
    history.register("fred", FredHistory())
    return handle


def test_a_read_returns_decimals_and_null_for_a_missing_value(fred, tmp_path):
    out = history.rate_daily("UST_3M", "2026-01-05", "2026-01-09", store=tmp_path)
    assert out.column("series").to_pylist() == ["UST_3M"] * 3
    assert out.column("rate").to_pylist() == [pytest.approx(0.0422), None, pytest.approx(0.0419)]
    assert out.column_names == list(RATE_DAILY.names)


def test_raw_keeps_the_vendors_strings_and_names_the_request(fred, tmp_path):
    history.rate_daily("UST_3M", "2026-01-05", "2026-01-09", store=tmp_path)
    files = store.table_dir(tmp_path, "fred", "rate_daily")
    raw = ds.dataset(files, partitioning="hive").to_table()
    assert set(raw.column("value").to_pylist()) == {"4.22", ".", "4.19"}
    assert set(raw.column("series_id").to_pylist()) == {"DGS3MO"}
    assert fred.calls[0][1]["series_id"] == "DGS3MO"


def test_naming_no_source_reaches_the_one_that_serves_the_table(fred):
    assert registry.default("rate_daily") == "fred"
    assert registry.default("equity_eod") == "thetadata"


def test_an_unknown_series_is_refused_by_name(fred, tmp_path):
    with pytest.raises(IngestFailed, match="unknown rate series 'UST_7Y'"):
        history.rate_daily("UST_7Y", "2026-01-05", "2026-01-09", store=tmp_path)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # Wednesday evening: Wednesday is over, held back two business days.
        (dt.datetime(2026, 10, 7, 18, 0, tzinfo=NY), D(2026, 10, 5)),
        # Wednesday morning: Tuesday is the last finished day.
        (dt.datetime(2026, 10, 7, 9, 0, tzinfo=NY), D(2026, 10, 2)),
        # Monday morning: Friday's value is not out until Monday afternoon.
        (dt.datetime(2026, 10, 5, 9, 0, tzinfo=NY), D(2026, 9, 30)),
        # Saturday: the week is over, the same as Friday evening.
        (dt.datetime(2026, 10, 10, 12, 0, tzinfo=NY), D(2026, 10, 7)),
    ],
)
def test_publication_is_held_back_two_business_days(now, expected):
    assert published_through(now) == expected


def test_a_refused_key_surfaces_freds_own_explanation(monkeypatch):
    body = json.dumps({"error_code": 400, "error_message": "api_key is not registered."})

    def refuse(*args, **kwargs):
        raise urllib.error.HTTPError("u", 400, "Bad Request", Message(), io.BytesIO(body.encode()))

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    handle = FredHandle(api_key="x", limiter=auth.limiter("fred", None))
    with pytest.raises(ConnectionFailed, match="HTTP 400: api_key is not registered"):
        handle.get("series/observations", series_id="DGS3MO")
