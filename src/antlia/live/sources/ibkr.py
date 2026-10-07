"""IBKR as a live source: a slice of an option chain, quoted now.

Read-only by construction: the session comes from `auth.session("ibkr")`,
which connects `readonly=True`, and the only requests made are contract
lookups and market-data snapshots.

The raw frame is one row per contract, built from ib_insync's objects under
**IBKR's own attribute names** -- `lastTradeDateOrContractMonth`, `bidSize`,
`modelGreeks.delta` -- with IBKR's own sentinels left in. The projection is
where `"20261016"` becomes a date and `-1.0` becomes NULL.

## Facts measured against the user's gateway (2026-08-29)

- **OPRA is on, so option quotes arrive.** Chain structure
  (`reqSecDefOptParams`) is free.
- **`modelGreeks` arrive as None.** Greeks need an underlying price and the
  account has no US equity realtime bundle; the error names the *stock's*
  feed, not OPRA. The columns are kept so they fill the day that changes.
- **The underlying is delayed.** The default strike band is centred on a
  delayed (`reqMarketDataType(3)`) price, which is plenty for choosing strikes
  and is never written into a quote row.
- **`bid`/`ask` of -1.0 is IBKR's "no quote"**, seen with the market closed;
  whether it appears in regular hours is unconfirmed. Either way it reads as
  NULL.

Not yet run against a live gateway: the request sequence below follows
ib_insync's documented API and is covered by a fake in the tests.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any
from zoneinfo import ZoneInfo

from antlia import auth
from antlia.live.base import LiveSource

NY = ZoneInfo("America/New_York")

#: Contracts per `reqTickers` call. Each snapshot holds a market-data line
#: until it answers, and the default allowance is 100 lines.
BATCH = 40

#: More than this is a scan, not a slice, and should be narrowed.
MAX_CONTRACTS = 400

#: The default strike band around the underlying: +/- 15%.
BAND = 0.15

_CONTRACT = (
    "conId",
    "symbol",
    "lastTradeDateOrContractMonth",
    "strike",
    "right",
    "multiplier",
    "exchange",
    "currency",
    "localSymbol",
    "tradingClass",
)
_TICKER = ("time", "bid", "bidSize", "ask", "askSize", "last", "lastSize", "volume", "close")
_GREEKS = ("impliedVol", "delta", "gamma", "vega", "theta", "undPrice", "optPrice", "pvDividend")


def _double(column: str) -> str:
    return f'TRY_CAST("{column}" AS DOUBLE)'


def _price(column: str) -> str:
    """Non-negative and finite, else NULL: drops -1, 1.797e308 and NaN alike."""
    x = _double(column)
    return f"CASE WHEN {x} >= 0 AND {x} < 1e300 THEN {x} END"


def _signed(column: str) -> str:
    """Finite, else NULL. A delta or theta may be negative."""
    x = _double(column)
    return f"CASE WHEN isfinite({x}) AND abs({x}) < 1e300 THEN {x} END"


def choose_expirations(
    listed: Sequence[str],
    today: dt.date,
    wanted: Sequence[str | dt.date] | None,
    min_dte: int | None,
    max_dte: int | None,
) -> list[str]:
    """IBKR's `YYYYMMDD` strings, filtered by an explicit list or a DTE band."""
    chosen = []
    asked: set[dt.date] = set()
    if wanted is not None:
        asked = {(w if isinstance(w, dt.date) else dt.date.fromisoformat(str(w))) for w in wanted}
    for raw in sorted(listed):
        expiry = dt.datetime.strptime(raw, "%Y%m%d").date()
        if wanted is not None and expiry not in asked:
            continue
        dte = (expiry - today).days
        if dte < 0:
            continue
        if min_dte is not None and dte < min_dte:
            continue
        if max_dte is not None and dte > max_dte:
            continue
        chosen.append(raw)
    return chosen


class IBKRLive(LiveSource):
    name = "ibkr"
    auth_source = "ibkr"
    extra = "ibkr"
    package = "ib_insync"
    datasets = frozenset({"option_quote"})

    def fetch(self, dataset: str, symbol: str, **options: Any) -> Any:
        if dataset != "option_quote":
            raise ValueError(f"{self.name} cannot serve {dataset!r}")
        from antlia.live.engine import pa

        ib_insync = self.require("ib_insync")
        asked = dt.datetime.now(dt.UTC)
        today = asked.astimezone(NY).date()

        with auth.session(self.auth_source, options.get("profile")) as ib:
            underlying = ib_insync.Stock(symbol, "SMART", "USD")
            if not ib.qualifyContracts(underlying):
                raise LookupError(f"ibkr does not recognise {symbol!r} as a US stock")
            params = ib.reqSecDefOptParams(
                underlying.symbol, "", underlying.secType, underlying.conId
            )
            chain = next((p for p in params if p.exchange == "SMART"), None)
            if chain is None:
                raise LookupError(f"ibkr lists no SMART option chain for {symbol!r}")

            expirations = choose_expirations(
                chain.expirations,
                today,
                options.get("expirations"),
                options.get("min_dte"),
                options.get("max_dte"),
            )
            band = options.get("strikes") or self._band(ib, underlying)
            strikes = [k for k in sorted(chain.strikes) if band[0] <= k <= band[1]]
            rights = [r for r in "CP" if r in options.get("rights", "CP").upper()]

            contracts = [
                ib_insync.Option(
                    symbol,
                    expiry,
                    strike,
                    right,
                    "SMART",
                    multiplier=chain.multiplier,
                    currency="USD",
                    tradingClass=chain.tradingClass,
                )
                for expiry in expirations
                for strike in strikes
                for right in rights
            ]
            if len(contracts) > MAX_CONTRACTS:
                raise ValueError(
                    f"{len(contracts)} contracts is a scan, not a slice (limit {MAX_CONTRACTS}); "
                    "narrow expirations, the DTE band or strikes=(lo, hi)"
                )
            if not contracts:
                return None

            # The union of strikes is wider than any one expiration's: a
            # combination that does not exist comes back unqualified.
            listed = [c for c in ib.qualifyContracts(*contracts) if getattr(c, "conId", 0)]
            ib.reqMarketDataType(1)
            tickers: list[Any] = []
            for i in range(0, len(listed), BATCH):
                tickers.extend(ib.reqTickers(*listed[i : i + BATCH]))

        if not tickers:
            return None
        rows = [_row(t) for t in tickers]
        frame = pa.Table.from_pylist(rows)
        stamp = pa.array([asked] * frame.num_rows, type=pa.timestamp("us", tz="UTC"))
        return frame.append_column("snapshot_at", stamp)

    def _band(self, ib: Any, underlying: Any) -> tuple[float, float]:
        """+/- BAND around a delayed underlying price. Delayed is enough to pick strikes."""
        ib.reqMarketDataType(3)
        try:
            [ticker] = ib.reqTickers(underlying)
        finally:
            ib.reqMarketDataType(1)
        price = ticker.marketPrice()
        if not (price == price and price > 0):  # NaN or unset
            price = ticker.close
        if not (price == price and price > 0):
            raise LookupError(
                f"no price for {underlying.symbol} to centre a strike band on; "
                "pass strikes=(lo, hi)"
            )
        return price * (1 - BAND), price * (1 + BAND)

    def projection(self, dataset: str) -> dict[str, str]:
        return {
            "date": "(\"snapshot_at\" AT TIME ZONE 'America/New_York')::DATE",
            "snapshot_at": '"snapshot_at"',
            "stamp": 'TRY_CAST("time" AS TIMESTAMPTZ)',
            "symbol": '"symbol"',
            "expiration": "strptime(\"lastTradeDateOrContractMonth\", '%Y%m%d')::DATE",
            "strike": _double("strike"),
            "right": 'upper(left("right", 1))',
            "bid": _price("bid"),
            "bid_size": _price("bidSize"),
            "ask": _price("ask"),
            "ask_size": _price("askSize"),
            "last": _price("last"),
            "volume": _price("volume"),
            "iv": _price("modelGreeks.impliedVol"),
            "delta": _signed("modelGreeks.delta"),
            "gamma": _signed("modelGreeks.gamma"),
            "vega": _signed("modelGreeks.vega"),
            "theta": _signed("modelGreeks.theta"),
            "underlying_price": _price("modelGreeks.undPrice"),
        }


def _row(ticker: Any) -> dict[str, Any]:
    """One ticker as IBKR named its fields. Nothing renamed, nothing repaired."""
    contract = ticker.contract
    row: dict[str, Any] = {name: getattr(contract, name, None) for name in _CONTRACT}
    row.update({name: getattr(ticker, name, None) for name in _TICKER})
    greeks = getattr(ticker, "modelGreeks", None)
    for name in _GREEKS:
        value = getattr(greeks, name, None) if greeks is not None else None
        row[f"modelGreeks.{name}"] = None if value is None else float(value)
    return row
