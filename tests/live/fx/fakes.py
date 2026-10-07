"""Doubles for the fx tests: a rate source, and a yfinance session to borrow."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from antlia.auth.base import Provider
from antlia.auth.errors import ConnectionFailed
from antlia.auth.spec import SourceSpec
from antlia.live.fx.base import RateSource
from antlia.live.fx.types import PIVOT, RateTable

STAMP = datetime(2026, 8, 28, 16, 0, tzinfo=UTC)


class FakeRates(RateSource):
    """A source that answers with what it was handed, or fails on cue."""

    def __init__(
        self,
        name: str,
        rates: dict[str, float] | None = None,
        fail: BaseException | None = None,
        as_of: datetime = STAMP,
    ) -> None:
        self.name = name
        self._rates = rates or {PIVOT: 1.0, "GBP": 0.75}
        self._fail = fail
        self._as_of = as_of
        self.calls = 0

    def rates(self, currencies: tuple[str, ...]) -> RateTable:
        self.calls += 1
        if self._fail is not None:
            raise self._fail
        # Answers exactly what was asked for, and refuses a partial answer --
        # the contract in `fx.base` that the real adapters are held to.
        missing = [c for c in currencies if c not in self._rates]
        if missing:
            raise ConnectionFailed(self.name, f"no rate for {', '.join(missing)}")
        return RateTable(PIVOT, {c: self._rates[c] for c in currencies}, self._as_of, self.name)


class FakeTicker:
    def __init__(self, frame: Any) -> None:
        self._frame = frame

    def history(self, period: str) -> Any:
        return self._frame


class FakeYFHandle:
    """Stands in for `auth`'s YFinanceHandle: `.ticker()` pays the limiter."""

    def __init__(self, frames: dict[str, Any]) -> None:
        self.frames = frames
        self.asked: list[str] = []

    def ticker(self, symbol: str) -> FakeTicker:
        self.asked.append(symbol)
        return FakeTicker(self.frames.get(symbol))


class FakeYFProvider(Provider):
    """Registered as `yfinance` in `auth`, so the fx source borrows it for real."""

    def __init__(self, handle: FakeYFHandle) -> None:
        self.spec = SourceSpec(name="yfinance", fields=(), identity=())
        self.handle = handle

    def connect(self, cred: Any, limiter: Any) -> FakeYFHandle:
        return self.handle


class BrokenYFProvider(Provider):
    spec = SourceSpec(name="yfinance", fields=(), identity=())

    def connect(self, cred: Any, limiter: Any) -> Any:
        raise ConnectionFailed("yfinance", "gateway down")
