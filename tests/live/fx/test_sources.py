"""The two adapters, against fake vendors -- especially the traps.

Each of these pins one thing that was expensive to discover: Yahoo's `{CCY}=X`
orientation, its empty-frame-instead-of-an-error habit, and Frankfurter's
Cloudflare User-Agent requirement.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest

from antlia import auth
from antlia.auth.errors import ConnectionFailed
from antlia.live.fx.sources.frankfurter import USER_AGENT, FrankfurterRates
from antlia.live.fx.sources.yfinance import YFinanceRates
from tests.live.fx.fakes import BrokenYFProvider, FakeYFHandle, FakeYFProvider

pd = pytest.importorskip("pandas")

STAMP = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)


def frame(close: float, stamp: datetime = STAMP):
    return pd.DataFrame({"Close": [close - 0.01, close]}, index=pd.DatetimeIndex([stamp, stamp]))


def yahoo(**pairs) -> FakeYFHandle:
    """Register a fake yfinance session in `auth` and hand back its handle."""
    handle = FakeYFHandle({symbol: value for symbol, value in pairs.items()})
    auth.register("yfinance", FakeYFProvider(handle))
    return handle


# -- yfinance ---------------------------------------------------------------


def test_ccy_x_is_units_per_one_usd_and_is_not_inverted():
    # `JPY=X` is 160.038 -- yen per dollar. Inverting it here would produce a
    # table that looks plausible and is wrong by a factor of 25,000.
    handle = yahoo(**{"GBP=X": frame(0.73869), "JPY=X": frame(160.038)})
    table = YFinanceRates().rates(("USD", "GBP", "JPY"))
    assert table.base == "USD"
    assert table.rates == {"USD": 1.0, "GBP": 0.73869, "JPY": 160.038}
    assert table.source == "yfinance"
    assert sorted(handle.asked) == ["GBP=X", "JPY=X"]


def test_the_pivot_is_never_fetched():
    handle = yahoo(**{"GBP=X": frame(0.73869)})
    YFinanceRates().rates(("USD", "GBP"))
    assert "USD=X" not in handle.asked


def test_the_pivot_alone_needs_no_call():
    handle = yahoo()
    table = YFinanceRates().rates(("USD",))
    assert table.rates == {"USD": 1.0}
    assert handle.asked == []


def test_an_empty_frame_is_a_failure_not_a_missing_row():
    # Yahoo answers an unavailable endpoint with an empty frame rather than an
    # error, so emptiness has to be read as failure or it becomes a silent gap.
    yahoo(**{"GBP=X": frame(0.73869), "JPY=X": pd.DataFrame({"Close": []})})
    with pytest.raises(ConnectionFailed, match="JPY"):
        YFinanceRates().rates(("USD", "GBP", "JPY"))


def test_a_partial_answer_is_refused_rather_than_returned():
    yahoo(**{"GBP=X": frame(0.73869)})
    with pytest.raises(ConnectionFailed, match="no rate for JPY"):
        YFinanceRates().rates(("USD", "GBP", "JPY"))


def test_a_nonpositive_close_is_refused():
    yahoo(**{"GBP=X": frame(0.0)})
    with pytest.raises(ConnectionFailed):
        YFinanceRates().rates(("USD", "GBP"))


def test_as_of_is_the_latest_bar_and_is_tz_aware():
    older = datetime(2026, 8, 27, tzinfo=UTC)
    yahoo(**{"GBP=X": frame(0.73869, STAMP), "JPY=X": frame(160.038, older)})
    table = YFinanceRates().rates(("USD", "GBP", "JPY"))
    assert table.as_of == STAMP
    assert table.as_of.tzinfo is not None


def test_a_dead_session_surfaces_as_connection_failed():
    auth.register("yfinance", BrokenYFProvider())
    with pytest.raises(ConnectionFailed, match="gateway down"):
        YFinanceRates().rates(("USD", "GBP"))


# -- frankfurter ------------------------------------------------------------


Requests = list[urllib.request.Request]


def respond(monkeypatch, payload: dict[str, object], captured: Requests | None = None):
    def urlopen(request, timeout=None):
        if captured is not None:
            captured.append(request)
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)


def test_ecb_rates_are_read_usd_based(monkeypatch):
    captured: Requests = []
    respond(monkeypatch, {"date": "2026-08-28", "rates": {"GBP": 0.73624}}, captured)
    table = FrankfurterRates().rates(("USD", "GBP"))
    assert table.rates == {"USD": 1.0, "GBP": 0.73624}
    assert table.source == "frankfurter"
    assert "base=USD" in captured[0].full_url and "symbols=GBP" in captured[0].full_url


def test_a_real_user_agent_is_sent(monkeypatch):
    # Cloudflare answers the default `Python-urllib/3.x` with a 403, so without
    # this header the fallback silently never fires -- the one failure mode a
    # fallback must not have.
    captured: Requests = []
    respond(monkeypatch, {"date": "2026-08-28", "rates": {"GBP": 0.73624}}, captured)
    FrankfurterRates().rates(("USD", "GBP"))
    agent = captured[0].get_header("User-agent")
    assert agent == USER_AGENT
    assert "urllib" not in agent


def test_as_of_is_the_fixing_not_the_fetch(monkeypatch):
    # The response dates the 16:00 CET fixing. Reporting `now` would claim a
    # freshness this source does not have, and hide the lag that put it second.
    respond(monkeypatch, {"date": "2026-08-28", "rates": {"GBP": 0.73624}})
    table = FrankfurterRates().rates(("USD", "GBP"))
    assert table.as_of.date() == datetime(2026, 8, 28).date()
    assert table.as_of.hour == 16
    assert table.as_of.tzinfo is not None


def test_a_missing_currency_is_refused(monkeypatch):
    respond(monkeypatch, {"date": "2026-08-28", "rates": {"GBP": 0.73624}})
    with pytest.raises(ConnectionFailed, match="no rate for JPY"):
        FrankfurterRates().rates(("USD", "GBP", "JPY"))


def test_an_unparseable_fixing_date_is_refused(monkeypatch):
    respond(monkeypatch, {"date": "whenever", "rates": {"GBP": 0.73624}})
    with pytest.raises(ConnectionFailed, match="unparseable fixing date"):
        FrankfurterRates().rates(("USD", "GBP"))


def test_a_403_names_the_user_agent_cause(monkeypatch):
    def urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 403, "Forbidden", email.message.Message(), None
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(ConnectionFailed, match="User-Agent"):
        FrankfurterRates().rates(("USD", "GBP"))


def test_a_transport_error_surfaces_as_connection_failed(monkeypatch):
    def urlopen(request, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(ConnectionFailed, match="TimeoutError"):
        FrankfurterRates().rates(("USD", "GBP"))


def test_frankfurter_needs_no_auth_source():
    # Keyless: no credential, no session, no client to yield. Registering it in
    # `auth` would mean inventing a wrapper purely to have something to return.
    assert FrankfurterRates().auth_source is None
    assert YFinanceRates().auth_source == "yfinance"


def test_verify_makes_one_real_round_trip(monkeypatch):
    respond(monkeypatch, {"date": "2026-08-28", "rates": {"GBP": 0.73624}})
    assert "0.73624" in FrankfurterRates().verify()
