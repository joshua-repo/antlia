"""ThetaData EOD history, through the session `antlia.auth` already owns.

Everything in this file is measured against a live key rather than read off a
docs page, because the vendor's limits are the whole shape of the ingest plan.

## What a FREE plan actually serves -- measured 2026-08-30

| call | result |
|---|---|
| `stock_history_eod` | works: OHLCV plus the closing NBBO |
| `option_history_eod` (`strike="*"`, `right="both"`) | works: a whole expiration in one request |
| `option_list_expirations` / `option_list_strikes` | works |
| everything intraday (`*_history_ohlc/quote/trade`) | PERMISSION_DENIED -- VALUE/STANDARD |
| `option_history_open_interest` | PERMISSION_DENIED -- needs VALUE |
| `option_history_greeks_eod` | PERMISSION_DENIED -- needs STANDARD |
| `option_list_contracts`, index, flat files | PERMISSION_DENIED |

So this adapter serves EOD and only EOD. That is not a simplification: it is
the entire free entitlement, and a table this source cannot fill is better
absent than half-populated.

## Five vendor facts the planner is built around

- **There is a history horizon, and crossing it fails the whole request.**
  A FREE plan reached back to **2023-07-10** when measured on 2026-08-30 (1147
  days, identical for stock and option EOD). A request whose *range* straddles
  the boundary does not get trimmed to what you may read -- it fails outright
  with PERMISSION_DENIED. So `earliest()` clamps before dispatch, and the
  refusal is still handled, because the boundary rolls and a constant cannot.
- **Today's EOD row does not exist until after the close.** Asked before
  then, the vendor answers for the rest of the window and says nothing about
  today -- and the ledger would settle today for good. `latest()` bounds the
  plan at `PUBLISHED_AT` New York time. The closing quote is *stamped*
  17:15-17:18; when the row becomes *readable* has not been measured, so the
  bound carries a margin. Too late is harmless (today waits for the next
  fill); too early brings the bug back for the minutes in between.
- **365 calendar days per request, inclusive.** 365 works, 366 is
  `INVALID_ARGUMENT: Too many days between start and end date`. Hence
  `max_span_days`.
- **Two concurrent requests, and the third is refused.** Measured: `--workers 2`
  completed six requests in 8.2s; `--workers 3` failed with *"Too many
  concurrent requests"*. Hence `max_workers`, which the ingest driver clamps to
  so a caller may ask for more without knowing.
- **A non-trading day raises `NoDataFoundError`, it does not return an empty
  frame.** Treating that as a failure would make every market holiday abort an
  ingest run; it is recorded as `empty` and never asked for again.
- **`strike="*"` fetches an entire expiration in one call.** 990 rows for one
  expiration over five sessions, in under six seconds. hawk's note that the API
  rejects strike lists and forces a per-strike thread pool does **not** apply
  to the 1.0.x SDK, and carrying that fan-out over would turn one request into
  hundreds.

Errors are classifiable: apart from the SDK's own two exceptions, everything
arrives as a `grpc.RpcError` carrying `.code()`, so "you may not read this" and
"the network broke" never get confused for one another.

`option_list_expirations`'s **first column is `symbol`, not the expiration** --
resolve columns by name, never by position.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any
from zoneinfo import ZoneInfo

from antlia import auth
from antlia.history.base import HistorySource, Scope, arrow, context
from antlia.history.engine import pa
from antlia.history.errors import IngestFailed, NotEntitled
from antlia.history.types import Window, as_date

#: The oldest date a FREE plan served when this was last measured. It is a
#: planning hint, not a rule: the window rolls, and the vendor's refusal at
#: fetch time is what actually decides. Override with $ANTLIA_THETADATA_EARLIEST
#: (or `earliest=` on the adapter) after re-running `--horizon`.
MEASURED_EARLIEST = dt.date(2023, 7, 10)

ENV_EARLIEST = "ANTLIA_THETADATA_EARLIEST"

#: Inclusive, and enforced by the vendor to the day.
MAX_SPAN_DAYS = 365

#: Measured 2026-08-30 on the FREE plan: two concurrent requests are served,
#: three are refused outright with "Too many concurrent requests". It is a
#: vendor rule rather than a preference, so the ingest driver clamps to it and
#: a caller asking for eight simply gets two.
MAX_WORKERS = 2

#: Anything before this is older than ThetaData's own option history and no
#: subscription reaches it. Used only to bound the horizon probe.
VENDOR_ORIGIN = dt.date(2012, 6, 1)

_NY = "America/New_York"

#: When the day's EOD row is treated as published, New York time. See the
#: module docstring: the stamp is 17:15-17:18, availability is unmeasured.
PUBLISHED_AT = dt.time(18, 0)


def published_through(now: dt.datetime) -> dt.date:
    """The newest session whose EOD row exists at `now` (tz-aware).

    A weekend or holiday is returned as-is: asking for it costs one request
    and settles as `empty`, which is true. Only a trading day before its
    summary exists is the dangerous case, and that is the one this excludes.
    """
    local = now.astimezone(ZoneInfo(_NY))
    if local.time() >= PUBLISHED_AT:
        return local.date()
    return local.date() - dt.timedelta(days=1)


#: The EOD columns ThetaData returns for both stocks and options, mapped onto
#: the canonical names. Applied by DuckDB over `raw/`, at read time.
_EOD = {
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
}

#: The session date. `created` is 17:15 New York on the session it summarises,
#: so the cast has to be done *in New York* -- a machine in Tokyo casting the
#: same instant lands on the next day.
_SESSION = f"(\"created\" AT TIME ZONE '{_NY}')::DATE"


class ThetaDataHistory(HistorySource):
    name = "thetadata"
    auth_source = "thetadata"
    extra = "thetadata"
    package = "thetadata"
    tables = frozenset({"equity_eod", "option_eod", "expirations"})
    max_span_days = MAX_SPAN_DAYS
    max_workers = MAX_WORKERS

    def __init__(self, earliest: dt.date | None = None) -> None:
        self._earliest = earliest

    # -- what this plan may read -------------------------------------------

    def earliest(self, table: str) -> dt.date | None:
        if self._earliest is not None:
            return self._earliest
        env = os.environ.get(ENV_EARLIEST)
        if env:
            return as_date(env)
        return MEASURED_EARLIEST

    def latest(self, table: str) -> dt.date | None:
        if table == "expirations":
            # A listing is about the symbol now, not a session: no lag.
            return None
        return published_through(dt.datetime.now(dt.UTC))

    def measure_earliest(self, symbol: str = "AAPL") -> dt.date:
        """Binary-search the live horizon. Costs about eleven requests.

        Worth running when the plan changes, or when denials start appearing
        for windows that used to work: the free window rolls forward, so a
        constant measured six months ago is six months too generous.
        """
        today = dt.date.today()
        lo, hi = 0, (today - VENDOR_ORIGIN).days
        with auth.session(self.auth_source) as client:
            while lo < hi:
                mid = (lo + hi + 1) // 2
                day = today - dt.timedelta(days=mid)
                try:
                    client.stock_history_eod(symbol, day, day)
                except Exception as exc:
                    if self._is_denial(exc):
                        hi = mid - 1
                        continue
                    # No data on a weekend is not a refusal: the window still
                    # reaches this far, so search older.
                    lo = mid
                    continue
                lo = mid
        return today - dt.timedelta(days=lo)

    # -- planning ----------------------------------------------------------

    def scopes(self, table: str, symbol: str, window: Window, **options: Any) -> list[Scope]:
        if table != "option_eod":
            return [Scope()]

        listed = options.get("expirations")
        if listed is None:
            listed = self.list_expirations(symbol)
        chosen = [as_date(e) for e in listed]

        # An expiration that expired before the window opened has nothing to
        # say about it; one that lists after the window closes has not traded
        # yet. Both are dropped here rather than discovered one request at a
        # time -- which is where the bulk of a warm-up's wasted calls go.
        chosen = [e for e in chosen if e >= window.start]
        max_dte = options.get("max_dte")
        if max_dte is not None:
            chosen = [e for e in chosen if (e - window.end).days <= int(max_dte)]
        min_dte = options.get("min_dte")
        if min_dte is not None:
            chosen = [e for e in chosen if (e - window.start).days >= int(min_dte)]

        return [
            Scope(key=e.isoformat(), bound=Window(VENDOR_ORIGIN, e)) for e in sorted(set(chosen))
        ]

    def list_expirations(self, symbol: str) -> list[dt.date]:
        """Every expiration the vendor lists for a symbol, oldest first."""
        frame = self._call(lambda c: c.option_list_expirations(symbol))
        table = arrow(frame)
        if table is None:
            return []
        # The first column is `symbol`, not the expiration. By name, always.
        return sorted({as_date(v) for v in table.column("expiration").to_pylist()})

    # -- fetching ----------------------------------------------------------

    def fetch(self, table: str, symbol: str, window: Window, scope: Scope) -> pa.Table | None:
        if table == "expirations":
            frame = arrow(self._call(lambda c: c.option_list_expirations(symbol)))
            return frame

        if table == "equity_eod":
            frame = arrow(
                self._call(lambda c: c.stock_history_eod(symbol, window.start, window.end))
            )
            # ThetaData's stock frame carries no symbol -- you told it the
            # symbol. Append it, or the file cannot say what it is about.
            return None if frame is None else context(frame, symbol=symbol)

        if table == "option_eod":
            expiration = scope.key
            frame = arrow(
                self._call(
                    lambda c: c.option_history_eod(
                        window.start,
                        window.end,
                        symbol,
                        expiration,
                        strike="*",
                        right="both",
                    )
                )
            )
            return frame

        raise IngestFailed(self.name, f"{self.name} cannot serve table {table!r}")

    def _call(self, fn: Any) -> Any:
        """One vendor request, paced and classified.

        The limiter is `auth`'s, process-wide and shared with every other
        ThetaData caller: a spec `rate` (or `rate_limit` in the credentials
        file) is what paces a long warm-up, and this adapter must not invent
        a second bucket beside it.
        """
        limiter = auth.limiter(self.auth_source)
        limiter.acquire()
        with auth.session(self.auth_source) as client:
            try:
                return fn(client)
            except Exception as exc:
                if self._is_no_data(exc):
                    return None
                if self._is_denial(exc):
                    raise NotEntitled(self.name, self._detail(exc)) from exc
                detail = self._detail(exc)
                if "concurrent" in detail.lower():
                    # Says what to change. The generic message would leave a
                    # reader guessing that `--workers` was the cause.
                    raise IngestFailed(
                        self.name,
                        f"{detail.strip()} -- lower --workers (this plan serves "
                        f"{MAX_WORKERS} at once)",
                    ) from exc
                raise IngestFailed(self.name, f"{type(exc).__name__}: {detail}") from exc

    # -- vendor error classification ---------------------------------------

    @staticmethod
    def _detail(exc: Exception) -> str:
        details = getattr(exc, "details", None)
        if callable(details):
            try:
                return str(details())
            except Exception:
                pass
        return str(exc)

    @staticmethod
    def _is_no_data(exc: Exception) -> bool:
        """A market holiday, or a contract that never traded. Not a failure."""
        return type(exc).__name__ == "NoDataFoundError"

    @staticmethod
    def _is_denial(exc: Exception) -> bool:
        """A plan boundary. Permanent, so the planner records it and moves on."""
        code = getattr(exc, "code", None)
        if callable(code):
            try:
                return str(code()) == "StatusCode.PERMISSION_DENIED"
            except Exception:
                return False
        return False

    # -- normalisation, at read time ---------------------------------------

    def projection(self, table: str) -> dict[str, str]:
        if table == "equity_eod":
            return {"date": _SESSION, "symbol": '"symbol"', **_EOD}
        if table == "option_eod":
            return {
                "date": _SESSION,
                "symbol": '"symbol"',
                "expiration": "strptime(\"expiration\", '%Y-%m-%d')::DATE",
                "strike": '"strike"',
                # The vendor spells these CALL/PUT; canonical is one letter.
                "right": (
                    "CASE upper(\"right\") WHEN 'CALL' THEN 'C' WHEN 'C' THEN 'C' "
                    "WHEN 'PUT' THEN 'P' WHEN 'P' THEN 'P' END"
                ),
                **_EOD,
            }
        if table == "expirations":
            return {
                "symbol": '"symbol"',
                "expiration": "strptime(\"expiration\", '%Y-%m-%d')::DATE",
            }
        raise KeyError(f"{self.name} has no projection for {table!r}")

    # -- doctor ------------------------------------------------------------

    def verify(self) -> str:
        expirations = self.list_expirations("AAPL")
        if not expirations:
            raise IngestFailed(self.name, "AAPL listed no expirations")
        horizon = self.earliest("option_eod")
        return (
            f"AAPL: {len(expirations)} expirations, "
            f"{expirations[0]} .. {expirations[-1]}; horizon {horizon}"
        )
