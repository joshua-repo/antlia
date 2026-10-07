"""A history source that answers from a script, and counts what it was asked.

The point of every test in this directory is that **the planner asks for the
least it can**, so a fake that records its calls is the instrument, not a
convenience. It mimics ThetaData's shape closely enough to be a fair test of
the projection: vendor-native column names, `CALL`/`PUT` spelled out, a
tz-aware `created` stamp at the New York close, and no `symbol` column on the
equity frame.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pyarrow as pa

from antlia.history.base import HistorySource, Scope, context
from antlia.history.errors import NotEntitled
from antlia.history.types import Window

NY = "America/New_York"

#: Two expirations, so an option plan has more than one scope to clamp.
EXPIRATIONS = (dt.date(2026, 1, 16), dt.date(2026, 2, 20))

_EOD_PROJECTION = {
    "open": '"open"',
    "high": '"high"',
    "low": '"low"',
    "close": '"close"',
    "volume": '"volume"',
    "trades": '"count"',
    "bid": '"bid"',
    "bid_size": '"bid_size"',
    "ask": '"ask"',
    "ask_size": '"ask_size"',
    "stamp": '"created"',
    "date": f"(\"created\" AT TIME ZONE '{NY}')::DATE",
}


class FakeHistory(HistorySource):
    """Deterministic EOD data, one row per weekday in the window."""

    name = "fake"
    tables = frozenset({"equity_eod", "option_eod", "expirations"})
    max_span_days = 10

    def __init__(
        self,
        *,
        earliest: dt.date | None = None,
        deny: bool = False,
        blank: bool = False,
        close: float = 1.5,
        expirations: tuple[dt.date, ...] | None = None,
        published: dt.date | None = None,
    ) -> None:
        # Passed in rather than read off a module global, so a test that needs
        # the vendor to list something new cannot leak that into the next one.
        self._expirations = tuple(expirations) if expirations is not None else EXPIRATIONS
        self._earliest = earliest
        self._deny = deny
        self._blank = blank
        self._close = close
        #: The last session the vendor has published. A public attribute so a
        #: test can move it forward, the way the real one moves every evening.
        self.published = published
        #: Every fetch, as `(table, symbol, window, scope)`. The assertions.
        self.calls: list[tuple[str, str, Window, str]] = []

    def earliest(self, table: str) -> dt.date | None:
        return self._earliest

    def latest(self, table: str) -> dt.date | None:
        # A listing is about the symbol today, not a session, so it has no
        # publication lag -- the same split as ThetaData's.
        return None if table == "expirations" else self.published

    def scopes(self, table: str, symbol: str, window: Window, **options: Any) -> list[Scope]:
        if table != "option_eod":
            return [Scope()]
        listed = options.get("expirations") or list(self._expirations)
        chosen = [e for e in listed if e >= window.start]
        if options.get("max_dte") is not None:
            chosen = [e for e in chosen if (e - window.end).days <= options["max_dte"]]
        return [Scope(e.isoformat(), Window(dt.date(2000, 1, 1), e)) for e in sorted(chosen)]

    def fetch(self, table: str, symbol: str, window: Window, scope: Scope) -> pa.Table | None:
        self.calls.append((table, symbol, window, scope.key))
        if self._deny:
            raise NotEntitled(self.name, "test plan does not cover this", window)
        if self._blank:
            return None
        if table == "expirations":
            return pa.table(
                {
                    "symbol": [symbol] * len(self._expirations),
                    "expiration": [e.isoformat() for e in self._expirations],
                }
            )
        days = [
            window.start + dt.timedelta(days=i) for i in range((window.end - window.start).days + 1)
        ]
        days = [d for d in days if d.weekday() < 5]
        if self.published is not None:
            # What a vendor does with a session it has not summarised yet: it
            # answers for the rest of the window and says nothing about that day.
            days = [d for d in days if d <= self.published]
        if not days:
            return None
        frame = pa.table(
            {
                # Naive stamps carrying the New York close, exactly as a vendor
                # frame arrives: the tz belongs to the type, not to the value.
                "created": pa.array(
                    [dt.datetime.combine(d, dt.time(17, 15)) for d in days],
                    type=pa.timestamp("ms", tz=NY),
                ),
                "open": [1.0] * len(days),
                "high": [2.0] * len(days),
                "low": [0.5] * len(days),
                "close": [self._close] * len(days),
                "volume": [10] * len(days),
                "count": [3] * len(days),
                "bid": [1.4] * len(days),
                "bid_size": [1] * len(days),
                "ask": [1.6] * len(days),
                "ask_size": [2] * len(days),
            }
        )
        if table == "option_eod":
            return (
                frame.append_column("symbol", pa.array([symbol] * len(days)))
                .append_column("expiration", pa.array([scope.key] * len(days)))
                .append_column("strike", pa.array([100.0] * len(days)))
                .append_column("right", pa.array(["CALL"] * len(days)))
            )
        # The equity frame deliberately carries no symbol, like ThetaData's.
        return context(frame, symbol=symbol)

    def projection(self, table: str) -> dict[str, str]:
        if table == "expirations":
            return {
                "symbol": '"symbol"',
                "expiration": "strptime(\"expiration\", '%Y-%m-%d')::DATE",
            }
        base = {"symbol": '"symbol"', **_EOD_PROJECTION}
        if table == "option_eod":
            base |= {
                "expiration": "strptime(\"expiration\", '%Y-%m-%d')::DATE",
                "strike": '"strike"',
                "right": "CASE upper(\"right\") WHEN 'CALL' THEN 'C' WHEN 'PUT' THEN 'P' END",
            }
        return base

    def verify(self) -> str:
        return "fake: fine"
