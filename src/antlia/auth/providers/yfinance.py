"""yfinance -- no credential, but still a source.

It goes through the same machinery for two reasons. It is rate limited in
practice (an unofficial endpoint that starts refusing under load), and giving
the unauthenticated source the same shape as the authenticated ones is what
stops "just this one is different" from becoming three special cases in every
consumer.

What `session("yfinance")` yields is a handle exposing the module and the
limiter, not the module itself. yfinance performs its network calls inside
`Ticker` methods, so there is no honest way to intercept them from the module
object -- pacing here is cooperative, and a handle that says so is better than a
wrapper that implies a guarantee it cannot keep.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType
from typing import Any

from antlia.auth.base import Provider
from antlia.auth.credentials import Credential
from antlia.auth.errors import ConnectionFailed
from antlia.auth.ratelimit import Limiter
from antlia.auth.spec import Field, SourceSpec


@dataclass(slots=True)
class YFinanceHandle:
    """The yfinance module plus the shared limiter.

    with session("yfinance") as yf:
        yf.acquire()
        hist = yf.module.Ticker("AAPL").history(period="1mo")
    """

    module: ModuleType
    limiter: Limiter

    def acquire(self, tokens: float = 1.0, timeout: float | None = None) -> bool:
        """Pace one upcoming call. Blocks; False only if `timeout` expired."""
        return self.limiter.acquire(tokens, timeout)

    def ticker(self, symbol: str) -> Any:
        """`yf.Ticker(symbol)` with the limiter already paid for the lookup."""
        self.acquire()
        return self.module.Ticker(symbol)


class YFinanceProvider(Provider):
    spec = SourceSpec(
        name="yfinance",
        fields=(Field("rate_limit", required=False, cast=float, doc="calls/sec override"),),
        extra="yfinance",
        package="yfinance",
        identity=(),
        rate=2.0,
        burst=5,
        doc="Yahoo Finance via yfinance. Unauthenticated, informally rate limited.",
    )

    def connect(self, cred: Credential, limiter: Limiter) -> YFinanceHandle:
        return YFinanceHandle(module=self.require("yfinance"), limiter=limiter)

    def verify(self, handle: Any) -> str:
        probe = "AAPL"
        try:
            bars = handle.ticker(probe).history(period="1d")
        except Exception as exc:
            raise ConnectionFailed("yfinance", f"{type(exc).__name__}: {exc}") from exc
        if bars is None or len(bars) == 0:
            raise ConnectionFailed(
                "yfinance",
                f"no data for {probe} -- Yahoo returns an empty frame rather than "
                "an error when it is throttling or the endpoint moved",
            )
        return f"{probe} {bars.index[-1].date()} close {float(bars['Close'].iloc[-1]):.2f}"

    def disconnect(self, handle: Any) -> None:
        return None
