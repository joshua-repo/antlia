"""live.option_chain against a fake gateway, and the record/read-back round trip."""

from __future__ import annotations

import contextlib
import datetime as dt
from dataclasses import dataclass, field
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from antlia import auth, history, live
from antlia.history.errors import IngestFailed
from antlia.live.sources import ibkr
from antlia.live.sources.ibkr import IBKRLive, choose_expirations
from antlia.schema import OPTION_QUOTE

NY = ZoneInfo("America/New_York")
TODAY = dt.datetime.now(dt.UTC).astimezone(NY).date()
NEAR = (TODAY + dt.timedelta(days=10)).strftime("%Y%m%d")
FAR = (TODAY + dt.timedelta(days=40)).strftime("%Y%m%d")
UNSET = 1.7976931348623157e308


@dataclass
class Stock:
    symbol: str
    exchange: str
    currency: str
    secType: str = "STK"
    conId: int = 0


@dataclass
class Option:
    symbol: str
    lastTradeDateOrContractMonth: str
    strike: float
    right: str
    exchange: str
    multiplier: str = ""
    currency: str = ""
    tradingClass: str = ""
    conId: int = 0
    localSymbol: str = ""


@dataclass
class Ticker:
    contract: object
    time: dt.datetime | None = None
    bid: float = float("nan")
    bidSize: float = float("nan")
    ask: float = float("nan")
    askSize: float = float("nan")
    last: float = float("nan")
    lastSize: float = float("nan")
    volume: float = float("nan")
    close: float = float("nan")
    modelGreeks: object = None
    price: float = 200.0

    def marketPrice(self):
        return self.price


@dataclass
class FakeIB:
    """The few `ib_insync.IB` calls the adapter makes, answering from a fixed chain."""

    strikes: list[float] = field(default_factory=lambda: [150.0, 190.0, 200.0, 210.0, 260.0])
    #: (expiration, strike) pairs that really exist. 260 is listed but never trades.
    exists: set[tuple[str, float]] = field(
        default_factory=lambda: {(e, k) for e in (NEAR, FAR) for k in (150.0, 190.0, 200.0, 210.0)}
    )
    types: list[int] = field(default_factory=list)
    asked: list[int] = field(default_factory=list)

    def qualifyContracts(self, *contracts):
        out = []
        for c in contracts:
            if isinstance(c, Stock):
                c.conId = 1
                out.append(c)
            elif (c.lastTradeDateOrContractMonth, c.strike) in self.exists:
                c.conId = hash((c.lastTradeDateOrContractMonth, c.strike, c.right)) & 0xFFFF
                c.localSymbol = f"{c.symbol} {c.lastTradeDateOrContractMonth}{c.right}{c.strike}"
                out.append(c)
        return out

    def reqSecDefOptParams(self, symbol, exchange, sec_type, con_id):
        smart = SimpleNamespace(
            exchange="SMART",
            expirations=[NEAR, FAR],
            strikes=self.strikes,
            multiplier="100",
            tradingClass=symbol,
        )
        return [SimpleNamespace(exchange="CBOE", expirations=[], strikes=[]), smart]

    def reqMarketDataType(self, kind):
        self.types.append(kind)

    def reqTickers(self, *contracts):
        self.asked.append(len(contracts))
        out = []
        for c in contracts:
            if isinstance(c, Stock):
                out.append(Ticker(contract=c))
                continue
            stamp = dt.datetime(2026, 10, 7, 19, 45, tzinfo=dt.UTC)
            greeks = SimpleNamespace(
                impliedVol=0.31,
                delta=-0.4 if c.right == "P" else 0.6,
                gamma=0.02,
                vega=0.1,
                theta=-0.05,
                undPrice=200.5,
                optPrice=3.1,
                pvDividend=0.0,
            )
            out.append(
                Ticker(
                    contract=c,
                    time=stamp,
                    # IBKR's "no quote" on the far 150 strike, and its "unset" size.
                    bid=-1.0 if c.strike == 150.0 else 3.0,
                    bidSize=UNSET if c.strike == 150.0 else 12.0,
                    ask=3.2,
                    askSize=8.0,
                    volume=40.0,
                    modelGreeks=greeks if c.strike == 200.0 else None,
                )
            )
        return out


@pytest.fixture
def gateway(monkeypatch):
    ib = FakeIB()

    @contextlib.contextmanager
    def session(*args, **kwargs):
        yield ib

    monkeypatch.setattr(auth, "session", session)
    fake_sdk = SimpleNamespace(Stock=Stock, Option=Option)
    monkeypatch.setattr(IBKRLive, "require", lambda self, module: fake_sdk)
    return ib


def test_the_snapshot_is_canonical_and_ordered_by_its_key(gateway):
    snap = live.option_chain("AAPL", max_dte=20, rights="C")
    table = snap.table
    assert table.column_names == [c.name for c in OPTION_QUOTE.columns]
    assert set(table.column("expiration").to_pylist()) == {
        dt.datetime.strptime(NEAR, "%Y%m%d").date()
    }
    assert set(table.column("right").to_pylist()) == {"C"}
    assert table.column("strike").to_pylist() == sorted(table.column("strike").to_pylist())


def test_the_default_band_centres_on_a_delayed_price_and_restores_realtime(gateway):
    snap = live.option_chain("AAPL", max_dte=20)
    # 200 +/- 15%: 150 and 260 are outside the band.
    assert set(snap.table.column("strike").to_pylist()) == {190.0, 200.0, 210.0}
    assert gateway.types[:2] == [3, 1]
    assert gateway.types[-1] == 1


def test_vendor_sentinels_become_null_and_greeks_keep_their_sign(gateway):
    snap = live.option_chain("AAPL", strikes=(140, 210), max_dte=20, rights="P")
    rows = {r["strike"]: r for r in snap.table.to_pylist()}
    assert rows[150.0]["bid"] is None and rows[150.0]["bid_size"] is None
    assert rows[190.0]["bid"] == 3.0
    assert rows[200.0]["delta"] == pytest.approx(-0.4)
    assert rows[200.0]["underlying_price"] == pytest.approx(200.5)
    assert rows[190.0]["delta"] is None


def test_raw_keeps_ibkrs_own_field_names(gateway):
    snap = live.option_chain("AAPL", max_dte=20)
    names = set(snap.raw.column_names)
    assert {"lastTradeDateOrContractMonth", "bidSize", "modelGreeks.delta", "conId"} <= names
    assert "expiration" not in names
    assert -1.0 not in snap.table.column("bid").to_pylist()


def test_a_listed_strike_that_never_trades_is_dropped_not_quoted(gateway):
    snap = live.option_chain("AAPL", strikes=(150, 300))
    assert 260.0 not in snap.table.column("strike").to_pylist()


def test_requests_are_batched_under_the_line_allowance(gateway, monkeypatch):
    monkeypatch.setattr(ibkr, "BATCH", 3)
    live.option_chain("AAPL", strikes=(140, 220))
    assert max(gateway.asked) <= 3


def test_a_scan_is_refused_before_any_quote_is_asked_for(gateway, monkeypatch):
    monkeypatch.setattr(ibkr, "MAX_CONTRACTS", 4)
    with pytest.raises(ValueError, match="a scan, not a slice"):
        live.option_chain("AAPL", strikes=(140, 220))
    assert gateway.asked == []


def test_a_recording_reads_back_exactly_as_it_was_taken(gateway, tmp_path):
    snap = live.option_chain("AAPL", strikes=(140, 220))
    assert history.record(snap, store=tmp_path) == snap.raw.num_rows
    day = snap.table.column("date")[0].as_py()
    kept = history.option_quotes("AAPL", day, day, store=tmp_path)
    assert kept.drop_columns(["source", "ingested_at"]).equals(snap.table)


def test_two_snapshots_are_two_samples_not_a_restatement(gateway, tmp_path):
    first = live.option_chain("AAPL", strikes=(190, 200), max_dte=20)
    second = live.option_chain("AAPL", strikes=(190, 200), max_dte=20)
    history.record(first, store=tmp_path)
    history.record(second, store=tmp_path)
    day = first.table.column("date")[0].as_py()
    kept = history.option_quotes("AAPL", day, day, store=tmp_path)
    assert kept.num_rows == first.rows + second.rows
    assert len(set(kept.column("snapshot_at").to_pylist())) == 2


@pytest.mark.parametrize("call", ["fill", "plan", "coverage"])
def test_a_recorded_table_has_no_plan_fill_or_coverage(call, tmp_path):
    with pytest.raises(ValueError, match="recorded, not fetched"):
        getattr(history, call)("option_quote", "AAPL", "2026-10-01", "2026-10-07", store=tmp_path)


def test_the_recorded_adapter_never_fetches():
    with pytest.raises(IngestFailed, match="history.record"):
        history.source("ibkr").fetch("option_quote", "AAPL", None, None)  # type: ignore[arg-type]


def test_expirations_are_chosen_by_list_or_by_dte_band():
    listed = ["20261016", "20261120", "20260918"]
    today = dt.date(2026, 10, 7)
    assert choose_expirations(listed, today, None, None, 30) == ["20261016"]
    assert choose_expirations(listed, today, None, 30, None) == ["20261120"]
    assert choose_expirations(listed, today, ["2026-11-20"], None, None) == ["20261120"]
