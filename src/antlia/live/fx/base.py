"""The rate-source contract.

A source turns one vendor's answer into a `RateTable`. It does one thing:
return **units of each requested currency per 1 USD**, complete, or raise.

Partial answers are refused on purpose. A source that quietly drops the one
currency it could not price hands the caller a table that converts three of
four positions and silently omits the fourth from a total -- so a source that
cannot cover the request fails, and the next source in the chain gets its turn.

An adapter authenticates *through* `antlia.auth` where the
source has a session at all, and never reimplements resolution, pooling or
rate limiting.
"""

from __future__ import annotations

import abc
import importlib
from datetime import UTC, datetime
from types import ModuleType

from antlia.auth.errors import MissingExtra
from antlia.live.fx.types import PIVOT, RateTable


def utc(stamp: datetime | None) -> datetime:
    """A tz-aware stamp. Naive input is read as UTC; None is now."""
    if stamp is None:
        return datetime.now(UTC)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def wanted(currencies: tuple[str, ...]) -> list[str]:
    """The currencies a source must actually fetch: everything but the pivot.

    The pivot is 1.0 by definition and no source is asked to price it.
    """
    return [c for c in dict.fromkeys(currencies) if c != PIVOT]


class RateSource(abc.ABC):
    """FX rates from one vendor."""

    #: Name of this source, as it appears in `RateTable.source` and the chain.
    name: str
    #: Source name in `antlia.auth`, when this one needs a session. None means
    #: it is a keyless public endpoint and there is nothing to authenticate.
    auth_source: str | None = None
    #: pip extra and import name, for a readable failure when the SDK is absent.
    extra: str | None = None
    package: str | None = None

    @abc.abstractmethod
    def rates(self, currencies: tuple[str, ...]) -> RateTable:
        """Every requested currency per 1 USD, or `RatesUnavailable`."""

    def verify(self) -> str:
        """One cheap real round-trip, for the doctor.

        Same three-level distinction `auth` draws: that a source is registered
        is not evidence it answers. This is what to debug against.
        """
        table = self.rates((PIVOT, "GBP"))
        return f"1 {table.base} = {table.rate('GBP'):.5f} GBP   as of {table.as_of:%Y-%m-%d %H:%M}"

    def require(self, module: str) -> ModuleType:
        try:
            return importlib.import_module(module)
        except ImportError as exc:
            raise MissingExtra(self.name, self.extra or self.name, self.package or module) from exc
