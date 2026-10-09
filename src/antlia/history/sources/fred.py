"""FRED as a history source: daily interest-rate series.

Serves one table, `rate_daily`. The canonical `series` is antlia's own name
(`UST_3M`), mapped here to FRED's id (`DGS3MO`), so a consumer never learns a
vendor's spelling and a second rate vendor can answer to the same names.

`raw/` keeps FRED's observation objects exactly as sent -- `realtime_start`,
`realtime_end`, `date`, `value`, all strings, `"."` for a missing value --
plus the two request parameters that say what the file is about: `series`
(antlia's) and `series_id` (FRED's). The percent-to-decimal conversion is in
the projection, at read time, where a mistake can be re-mapped.

## Vendor facts

- **A rate is published the business day after it is observed**, mid
  afternoon New York (H.15 at about 16:15). Measured 2026-10-07: DGS3MO
  reached only 2026-10-05 while SOFR already had 2026-10-06. Asked too early,
  FRED returns the window without that day, and the ledger would settle it.
  `latest()` is therefore conservative: two business days before the last
  business day whose 17:00 New York has passed. Too late costs a day of
  waiting; too early loses the day for good.
- **A missing observation is the string `"."`**, not an absent row. It stays
  in `raw/` and projects to NULL -- an unreadable value is None, never 0.
- **No span cap worth planning around**: one request returns up to 100,000
  observations, decades of a daily series.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

from antlia import auth
from antlia.auth.errors import ConnectionFailed
from antlia.history.base import HistorySource, Scope, context
from antlia.history.engine import pa
from antlia.history.errors import IngestFailed
from antlia.history.types import Window

#: antlia's series name -> FRED's id. Each is a daily series; the basis is
#: the series' own and is noted, because a discount yield and a bond-equivalent
#: yield are not the same number.
SERIES: dict[str, str] = {
    "UST_1M": "DGS1MO",  # Treasury constant maturity, investment basis
    "UST_3M": "DGS3MO",
    "UST_6M": "DGS6MO",
    "UST_1Y": "DGS1",
    "UST_2Y": "DGS2",
    "UST_10Y": "DGS10",
    "TBILL_3M": "DTB3",  # 3-month bill, secondary market, discount basis
    "SOFR": "SOFR",
    "EFFR": "EFFR",
}

_NY = "America/New_York"

#: When a business day counts as over, New York time.
CLOSE = dt.time(17, 0)

#: Business days held back from the last finished one. One for FRED's
#: next-day publication, one for a holiday in between.
LAG_BUSINESS_DAYS = 2


def _previous_business_day(day: dt.date) -> dt.date:
    day -= dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day


def published_through(now: dt.datetime) -> dt.date:
    """The newest observation date it is safe to treat as published at `now`."""
    local = now.astimezone(ZoneInfo(_NY))
    day = local.date()
    if local.time() < CLOSE or day.weekday() >= 5:
        day = _previous_business_day(day)
    for _ in range(LAG_BUSINESS_DAYS):
        day = _previous_business_day(day)
    return day


class FredHistory(HistorySource):
    name = "fred"
    auth_source = "fred"
    tables = frozenset({"rate_daily"})

    def latest(self, table: str) -> dt.date | None:
        return published_through(dt.datetime.now(dt.UTC))

    def scopes(self, table: str, symbol: str, window: Window, **options: Any) -> list[Scope]:
        return [Scope()]

    def fetch(self, table: str, symbol: str, window: Window, scope: Scope) -> pa.Table | None:
        if table != "rate_daily":
            raise IngestFailed(self.name, f"{self.name} cannot serve table {table!r}")
        series_id = fred_id(symbol)
        try:
            with auth.session(self.auth_source) as fred:
                payload = fred.get(
                    "series/observations",
                    series_id=series_id,
                    observation_start=window.start.isoformat(),
                    observation_end=window.end.isoformat(),
                )
        except ConnectionFailed as exc:
            raise IngestFailed(self.name, str(exc)) from exc
        observations = payload.get("observations") or []
        if not observations:
            return None
        frame = pa.Table.from_pylist(observations)
        return context(frame, series=symbol, series_id=series_id)

    def projection(self, table: str) -> dict[str, str]:
        return {
            "date": 'CAST("date" AS DATE)',
            "series": '"series"',
            "rate": "TRY_CAST(NULLIF(\"value\", '.') AS DOUBLE) / 100",
        }

    def verify(self) -> str:
        return auth.verify(self.auth_source)


def fred_id(series: str) -> str:
    """FRED's id for one of antlia's series names. Unknown names are refused."""
    try:
        return SERIES[series]
    except KeyError:
        known = ", ".join(sorted(SERIES))
        raise IngestFailed("fred", f"unknown rate series {series!r}; known: {known}") from None
