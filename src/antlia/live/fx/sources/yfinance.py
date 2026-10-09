"""Yahoo Finance FX, through the session `antlia.auth` already owns.

`{CCY}=X` is **units of that currency per 1 USD** -- `JPY=X` is 160.04, `GBP=X`
is 0.7387 -- which is exactly the pivot orientation, so no arithmetic happens
here beyond reading the last close.

Why this is first in the chain: it is a market rate, and it reconciles with the
broker. On 2026-08-29 Yahoo said USD/JPY 160.04 against IBKR's own reported
160.08, and a JPY 772,600 position converted to USD 4,827.60 against IBKR's own
USD 4,826. That agreement is the point. A converted total that cannot be
reconciled with the broker's own screen is worthless, whatever its provenance.

Two vendor facts that cost time to rediscover:

- **`fast_info` returns `None` for FX pairs.** Only `history()` answers, and
  its last close is the most recent rate Yahoo will admit to.
- **Yahoo answers an unavailable endpoint with an empty frame, not an error.**
  So emptiness is treated as failure here, exactly as `auth`'s yfinance
  `verify()` does.

The session comes from `auth.session("yfinance")`: this module never imports
yfinance itself, and so inherits the shared token bucket rather than opening a
second, uncoordinated one. The pairs are fetched in parallel because they are
independent HTTP calls; the limiter is process-wide and thread-safe, so the
pool paces itself against every other yfinance caller in the program.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from antlia import auth
from antlia.live.fx.base import RateSource, utc, wanted
from antlia.live.fx.errors import RatesUnavailable
from antlia.live.fx.types import PIVOT, RateTable

#: Enough history that a weekend or a holiday still yields a close.
PERIOD = "5d"

#: Independent HTTP calls, but not a reason to open twenty sockets at once.
MAX_WORKERS = 8


class YFinanceRates(RateSource):
    name = "yfinance"
    auth_source = "yfinance"
    extra = "yfinance"
    package = "yfinance"

    def _one(self, handle: Any, currency: str) -> tuple[str, float, datetime] | None:
        """One pair's last close, or None if Yahoo had nothing to say."""
        frame = handle.ticker(f"{currency}=X").history(period=PERIOD)
        if frame is None or len(frame) == 0:
            return None
        close = float(frame["Close"].iloc[-1])
        if close <= 0:
            return None
        return currency, close, utc(frame.index[-1].to_pydatetime())

    def rates(self, currencies: tuple[str, ...]) -> RateTable:
        needed = wanted(currencies)
        if not needed:
            # Asked for the pivot alone: true by definition, no call to make.
            return RateTable(PIVOT, {PIVOT: 1.0}, utc(None), self.name)

        with auth.session(self.name) as handle:
            try:
                with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(needed))) as pool:
                    found = [r for r in pool.map(lambda c: self._one(handle, c), needed) if r]
            except Exception as exc:
                raise RatesUnavailable(self.name, f"{type(exc).__name__}: {exc}") from exc

        if len(found) != len(needed):
            missing = ", ".join(sorted(set(needed) - {c for c, _, _ in found}))
            # Partial is refused, not returned: see `fx.base`. The next source
            # in the chain gets a turn instead of the caller getting a table
            # that silently drops a currency out of a total.
            raise RatesUnavailable(
                self.name,
                f"no rate for {missing} -- Yahoo returns an empty frame rather "
                "than an error when it is throttling or the symbol moved",
            )

        rates = {PIVOT: 1.0} | {currency: rate for currency, rate, _ in found}
        return RateTable(
            base=PIVOT,
            rates=rates,
            as_of=max(stamp for _, _, stamp in found),
            source=self.name,
        )
