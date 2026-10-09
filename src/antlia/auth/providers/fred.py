"""FRED -- the St. Louis Fed's economic data API. A free key, and no SDK.

There is no vendor client to yield, so `session("fred")` yields the smallest
thing that can honestly be one: the key, the shared limiter, and a `get()`
that makes one paced HTTP request and returns the vendor's JSON untouched.
It interprets nothing in the payload -- `history`'s adapter does that -- which
keeps it on the authentication side of the line the other providers hold.

Facts that cost time to rediscover:

- **The keyless CSV endpoint is not used**, deliberately. `fredgraph.csv`
  names its value column after the series (`observation_date,DGS3MO`), so
  every series lands in `raw/` under a different column name and one canonical
  projection cannot read them all without renaming on the way in. The keyed
  API returns the same four columns for every series.
- **A bad or missing key is HTTP 400 with a JSON body**, `error_message`
  naming the problem. It is surfaced verbatim.
- **120 requests a minute** is the published limit, hence `rate=2.0`.

Get a key at https://fred.stlouisfed.org/docs/api/api_key.html.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from antlia.auth.base import Provider
from antlia.auth.credentials import Credential
from antlia.auth.errors import ConnectionFailed
from antlia.auth.ratelimit import Limiter
from antlia.auth.spec import Field, SourceSpec

BASE_URL = "https://api.stlouisfed.org/fred/"
TIMEOUT = 30.0


@dataclass(slots=True)
class FredHandle:
    """The key, the limiter, and one paced request.

    with session("fred") as fred:
        payload = fred.get("series/observations", series_id="DGS3MO")
    """

    api_key: str
    limiter: Limiter
    base_url: str = BASE_URL

    def get(self, endpoint: str, **params: Any) -> dict[str, Any]:
        """One GET, paced by the shared bucket. The vendor's JSON, unaltered."""
        query = urllib.parse.urlencode({**params, "api_key": self.api_key, "file_type": "json"})
        url = f"{self.base_url}{endpoint}?{query}"
        self.limiter.acquire()
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
                payload: dict[str, Any] = json.load(response)
        except urllib.error.HTTPError as exc:
            raise ConnectionFailed("fred", _explain(exc)) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ConnectionFailed("fred", f"{type(exc).__name__}: {exc}") from exc
        return payload


def _explain(exc: urllib.error.HTTPError) -> str:
    """FRED's own words for a refusal, which name the cause; else the status."""
    try:
        body = json.loads(exc.read())
        message = body.get("error_message")
    except Exception:
        message = None
    return f"HTTP {exc.code}: {message}" if message else f"HTTP {exc.code}"


class FredProvider(Provider):
    spec = SourceSpec(
        name="fred",
        fields=(
            Field("api_key", secret=True, doc="free, from fred.stlouisfed.org"),
            Field("rate_limit", required=False, cast=float, doc="calls/sec override"),
        ),
        identity=("api_key",),
        rate=2.0,
        burst=5,
        doc="St. Louis Fed FRED: interest-rate and economic series. Keyed, no SDK.",
    )

    def connect(self, cred: Credential, limiter: Limiter) -> FredHandle:
        return FredHandle(api_key=cred["api_key"], limiter=limiter)

    def verify(self, handle: Any) -> str:
        payload = handle.get("series", series_id="DGS3MO")
        series = payload.get("seriess") or []
        if not series:
            raise ConnectionFailed("fred", "the key was accepted but DGS3MO came back empty")
        latest = series[0].get("observation_end", "?")
        return f"DGS3MO listed through {latest}"
