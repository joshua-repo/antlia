"""Live account state -- positions, balances, margin, orders, fills.

    from antlia import account

    snap = account.snapshot("ibkr")
    snap.margin.excess_liquidity
    [p for p in snap.positions if p.instrument.kind == "option"]

One canonical schema, whichever broker it came from: `antlia.account.types` is
what consumers see, not the vendor's shapes. Authentication, the connection and
its lifetime belong to `antlia.auth`, which this layer borrows rather than
reimplements.

Two boundaries hold here:

- **Read-only.** Reading working orders is in scope; placing one never is, and
  the IBKR session connects `readonly=True` so the broker enforces it.
- **No writes to the store.** Account state is stale the moment it is read, and
  persisting it is the batch ingest path's job, not this one's.

Account state shares no types with historical data on purpose -- one is
immutable-by-date, the other expires on arrival.
"""

from __future__ import annotations

from typing import Any

from antlia.account import registry
from antlia.account.registry import register, sources, unregister
from antlia.account.types import (
    BASE,
    AccountSnapshot,
    Balance,
    Fill,
    Instrument,
    Margin,
    Order,
    Position,
)


def source(name: str) -> Any:
    """The adapter for `name`, imported on first use."""
    return registry.get(name)


def accounts(name: str, profile: str | None = None, **overrides: Any) -> list[str]:
    """Account ids reachable with these credentials."""
    return registry.get(name).accounts(profile, **overrides)


def snapshot(
    name: str, account: str | None = None, profile: str | None = None, **overrides: Any
) -> AccountSnapshot:
    """Everything one source can say about one account, at one instant."""
    return registry.get(name).snapshot(account, profile, **overrides)


def positions(
    name: str, account: str | None = None, profile: str | None = None, **overrides: Any
) -> tuple[Position, ...]:
    return registry.get(name).positions(account, profile, **overrides)


def balances(
    name: str, account: str | None = None, profile: str | None = None, **overrides: Any
) -> tuple[Balance, ...]:
    return registry.get(name).balances(account, profile, **overrides)


def margin(
    name: str, account: str | None = None, profile: str | None = None, **overrides: Any
) -> Margin | None:
    return registry.get(name).margin(account, profile, **overrides)


def orders(
    name: str, account: str | None = None, profile: str | None = None, **overrides: Any
) -> tuple[Order, ...]:
    return registry.get(name).orders(account, profile, **overrides)


def fills(
    name: str, account: str | None = None, profile: str | None = None, **overrides: Any
) -> tuple[Fill, ...]:
    return registry.get(name).fills(account, profile, **overrides)


__all__ = [
    "accounts",
    "snapshot",
    "positions",
    "balances",
    "margin",
    "orders",
    "fills",
    "source",
    "register",
    "unregister",
    "sources",
    "AccountSnapshot",
    "Balance",
    "Fill",
    "Instrument",
    "Margin",
    "Order",
    "Position",
    "BASE",
]
