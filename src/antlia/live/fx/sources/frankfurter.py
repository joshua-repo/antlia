"""Frankfurter -- the ECB's reference rates, keyless and quota-free.

One request, `base` handled server-side, no account and no SDK. It is the
fallback rather than the primary for one reason: it is a **once-daily 16:00 CET
fixing**, so it can sit most of a day behind the market. Measured 2026-08-28,
GBP/USD 0.73624 against Yahoo's 0.73869 -- 0.33%, which is small enough to look
right and large enough to break a reconciliation against a broker's screen.

It is here anyway because a chain whose only link is an unofficial scraper is
not a chain. yfinance periodically breaks (its `fast_info` already returns
`None` for FX pairs), and an official, stable, keyless source underneath it is
what turns that from an outage into a footnote.

**The User-Agent is not optional.** The service sits behind Cloudflare, which
answers urllib's default `Python-urllib/3.x` with a 403. Without this header
the fallback silently never fires -- which is the one failure mode a fallback
must not have, because nothing surfaces until the primary is already down.

Why this is not an `antlia.auth` source: `auth` answers "give me a working
client for source X", and yields the vendor's own client rather than a wrapper.
Frankfurter has no credential, no session and no client -- registering it would
mean inventing a wrapper object purely to have something to yield, which is the
boundary `auth` exists to hold. It still shares the process-wide token bucket,
so politeness is not reimplemented here either.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from antlia.auth.ratelimit import endpoint_limiter
from antlia.live.fx.base import RateSource, wanted
from antlia.live.fx.errors import RatesUnavailable
from antlia.live.fx.types import PIVOT, RateTable

URL = "https://api.frankfurter.dev/v1/latest"
TIMEOUT = 15.0

#: No published quota, so this is politeness rather than a limit: the data
#: changes once a day and nothing here needs it faster.
RATE = 1.0
BURST = 3

#: Identifying, and enough to get past Cloudflare. See the module docstring.
USER_AGENT = "antlia/0.1 (+https://github.com/joshua-repo/antlia)"

#: The fixing is published at 16:00 CET. `Europe/Berlin` gives the right hour
#: across the summer-time boundary; a slim image without tzdata falls back to
#: UTC+1, which mislabels the stamp by an hour and never the rate.
FIXING_HOUR = 16


def _cet() -> Any:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Europe/Berlin")
    except Exception:
        return timezone(timedelta(hours=1))


class FrankfurterRates(RateSource):
    name = "frankfurter"
    #: Keyless: nothing to authenticate, so no `auth` source backs this one.
    auth_source = None

    def _fetch(self, symbols: list[str]) -> dict[str, Any]:
        url = f"{URL}?base={PIVOT}&symbols={','.join(symbols)}"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        endpoint_limiter(self.name, None, "/v1/latest", RATE, BURST).acquire()
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                payload: dict[str, Any] = json.load(response)
        except urllib.error.HTTPError as exc:
            hint = " -- Cloudflare rejects a default urllib User-Agent" if exc.code == 403 else ""
            raise RatesUnavailable(self.name, f"HTTP {exc.code}{hint}") from exc
        except Exception as exc:
            raise RatesUnavailable(self.name, f"{type(exc).__name__}: {exc}") from exc
        return payload

    def rates(self, currencies: tuple[str, ...]) -> RateTable:
        needed = wanted(currencies)
        if not needed:
            return RateTable(PIVOT, {PIVOT: 1.0}, datetime.now(_cet()), self.name)

        payload = self._fetch(needed)
        quoted = payload.get("rates") or {}

        rates = {PIVOT: 1.0}
        for currency in needed:
            rate = quoted.get(currency)
            if not rate:
                # Partial is refused, not returned: see `fx.base`.
                raise RatesUnavailable(self.name, f"no rate for {currency}")
            rates[currency] = float(rate)

        # The response dates the *fixing*, not the fetch, so the stamp is built
        # from the date it names. Reporting `now` would claim a freshness this
        # source does not have, and hide exactly the lag that put it second.
        try:
            day = datetime.fromisoformat(str(payload["date"]))
        except (KeyError, ValueError) as exc:
            named = payload.get("date")
            raise RatesUnavailable(self.name, f"unparseable fixing date {named!r}") from exc
        as_of = day.replace(hour=FIXING_HOUR, minute=0, tzinfo=_cet())

        return RateTable(base=PIVOT, rates=rates, as_of=as_of, source=self.name)
