"""The account-source contract.

An adapter turns one broker's live account state into the canonical types. It
reads and only reads: nothing here writes to the store (that is `history`'s
single ingest path) and nothing here places an order.

`snapshot()` has a default implementation that composes the parts, so an adapter
only overrides it when the broker offers a cheaper one-shot call.
"""

from __future__ import annotations

import abc
import importlib
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

from antlia.account.types import (
    AccountSnapshot,
    Balance,
    Fill,
    Margin,
    Order,
    Position,
)
from antlia.auth.errors import MissingExtra


def now() -> datetime:
    return datetime.now(UTC)


class AccountSource(abc.ABC):
    """Live account state from one broker."""

    #: Source name in `antlia.auth` -- the adapter authenticates through it
    #: rather than reimplementing credentials.
    name: str
    #: pip extra and import name, for a readable failure when the SDK is absent.
    extra: str | None = None
    package: str | None = None

    @abc.abstractmethod
    def accounts(self, profile: str | None = None, **overrides: Any) -> list[str]:
        """Account ids reachable with these credentials."""

    @abc.abstractmethod
    def snapshot(
        self, account: str | None = None, profile: str | None = None, **overrides: Any
    ) -> AccountSnapshot:
        """Everything, at one instant. The other readers are views of this."""

    def positions(
        self, account: str | None = None, profile: str | None = None, **overrides: Any
    ) -> tuple[Position, ...]:
        return self.snapshot(account, profile, **overrides).positions

    def balances(
        self, account: str | None = None, profile: str | None = None, **overrides: Any
    ) -> tuple[Balance, ...]:
        return self.snapshot(account, profile, **overrides).balances

    def margin(
        self, account: str | None = None, profile: str | None = None, **overrides: Any
    ) -> Margin | None:
        return self.snapshot(account, profile, **overrides).margin

    def orders(
        self, account: str | None = None, profile: str | None = None, **overrides: Any
    ) -> tuple[Order, ...]:
        return self.snapshot(account, profile, **overrides).orders

    def fills(
        self, account: str | None = None, profile: str | None = None, **overrides: Any
    ) -> tuple[Fill, ...]:
        return self.snapshot(account, profile, **overrides).fills

    def require(self, module: str) -> ModuleType:
        try:
            return importlib.import_module(module)
        except ImportError as exc:
            raise MissingExtra(self.name, self.extra or self.name, self.package or module) from exc


def to_float(raw: object) -> float | None:
    """Broker numerics arrive as strings, sometimes empty or non-numeric.

    A tag whose value cannot be read is None, never 0.0 -- a zero that means
    "unknown" is the kind of thing that silently becomes a wrong risk number.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return float(raw)
    text = str(raw).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None
