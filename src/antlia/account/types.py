"""The canonical account schema.

`raw/` stores vendor-native; the read API returns one shape. This module is that
shape for account state -- the types every consumer sees, whichever broker the
data came from. Without it each dashboard reimplements per-vendor mapping and
"single source of truth" has been moved rather than achieved.

Account state is **stale the moment it is read**, which is why these types are
frozen and carry `as_of`: they are a snapshot with a timestamp, not a handle to
something live. They deliberately share nothing with the historical types.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

#: Instrument classes, normalised across brokers.
STOCK = "stock"
OPTION = "option"
FUTURE = "future"
FUTURE_OPTION = "future_option"
FOREX = "forex"
INDEX = "index"
FUND = "fund"
BOND = "bond"
CRYPTO = "crypto"
COMBO = "combo"
OTHER = "other"

#: The consolidated, base-currency view a broker reports alongside the real
#: currencies. It is a rollup, not a currency -- summing over balances without
#: excluding it double-counts.
BASE = "BASE"


@dataclass(frozen=True, slots=True)
class Instrument:
    """What a position is in.

    `ids` carries the vendor's own identifiers verbatim (`ibkr_conid`, the OCC
    local symbol, ...). It deliberately does **not** resolve identity across
    sources -- that is an open design question, and inventing a synthetic key
    here would bury the problem instead of leaving it visible. Preserving the
    vendor ids is what makes a real resolver possible later.
    """

    symbol: str
    kind: str
    currency: str
    exchange: str | None = None
    expiry: date | None = None
    strike: float | None = None
    right: str | None = None
    multiplier: float = 1.0
    local_symbol: str | None = None
    ids: Mapping[str, str] = field(default_factory=dict)

    @property
    def is_derivative(self) -> bool:
        return self.kind in (OPTION, FUTURE, FUTURE_OPTION)

    def __str__(self) -> str:
        if self.kind == OPTION and self.expiry and self.strike is not None:
            return f"{self.symbol} {self.expiry:%Y-%m-%d} {self.strike:g}{self.right or ''}"
        return self.local_symbol or self.symbol


@dataclass(frozen=True, slots=True)
class Position:
    """One holding.

    **`average_price` and `market_price` are on the same scale**: both are per
    quoted unit, with the contract multiplier divided out. Brokers do not
    guarantee this -- IBKR reports `avgCost` inclusive of the multiplier while
    `marketPrice` excludes it, so an option bought at 2.90 arrives as 290.04
    against a market price of 3.50. Comparing those two numbers is a mistake
    every consumer would otherwise make once. Multiply by `multiplier` (or use
    `cost_basis`) to get money.
    """

    account: str
    source: str
    instrument: Instrument
    quantity: float
    average_price: float | None
    market_price: float | None
    market_value: float | None
    unrealized_pnl: float | None
    realized_pnl: float | None
    currency: str
    as_of: datetime

    @property
    def cost_basis(self) -> float | None:
        """Money paid, signed with the position."""
        if self.average_price is None:
            return None
        return self.average_price * self.quantity * self.instrument.multiplier

    @property
    def is_short(self) -> bool:
        return self.quantity < 0


@dataclass(frozen=True, slots=True)
class Balance:
    """Cash and P&L in one currency.

    A multi-currency account produces one of these per currency, plus one for
    `BASE` -- the broker's consolidated view. Filter `BASE` out before summing.
    """

    account: str
    source: str
    currency: str
    cash: float | None = None
    settled_cash: float | None = None
    accrued_interest: float | None = None
    net_liquidation: float | None = None
    unrealized_pnl: float | None = None
    realized_pnl: float | None = None
    exchange_rate: float | None = None
    as_of: datetime | None = None

    @property
    def is_consolidated(self) -> bool:
        return self.currency == BASE


@dataclass(frozen=True, slots=True)
class Margin:
    """Requirements and headroom, in the account's base currency.

    `look_ahead_*` are the values after the next margin change (an expiry, a
    settlement), which is the number that matters for whether a position
    survives the night. `full_*` ignore intraday relief.
    """

    account: str
    source: str
    currency: str
    as_of: datetime
    net_liquidation: float | None = None
    equity_with_loan: float | None = None
    gross_position_value: float | None = None
    total_cash: float | None = None
    accrued_cash: float | None = None
    initial_margin: float | None = None
    maintenance_margin: float | None = None
    available_funds: float | None = None
    excess_liquidity: float | None = None
    buying_power: float | None = None
    cushion: float | None = None
    leverage: float | None = None
    full_initial_margin: float | None = None
    full_maintenance_margin: float | None = None
    look_ahead_initial_margin: float | None = None
    look_ahead_maintenance_margin: float | None = None
    look_ahead_excess_liquidity: float | None = None
    day_trades_remaining: float | None = None

    @property
    def utilisation(self) -> float | None:
        """Maintenance margin as a fraction of net liquidation."""
        if not self.net_liquidation or self.maintenance_margin is None:
            return None
        return self.maintenance_margin / self.net_liquidation


@dataclass(frozen=True, slots=True)
class Order:
    """A working order. Reading them is in scope; placing them never is."""

    account: str
    source: str
    order_id: str
    instrument: Instrument
    side: str
    quantity: float
    filled: float = 0.0
    remaining: float | None = None
    order_type: str | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: str | None = None
    status: str | None = None
    as_of: datetime | None = None
    ids: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Fill:
    """One execution, with its commission where the broker reports it."""

    account: str
    source: str
    fill_id: str
    instrument: Instrument
    side: str
    quantity: float
    price: float
    time: datetime
    commission: float | None = None
    commission_currency: str | None = None
    realized_pnl: float | None = None
    ids: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Everything one source can say about one account at one instant.

    `vendor` keeps whatever did not map onto the canonical fields, vendor-native
    and unaltered. It is an escape hatch for the long tail, not a second API:
    reaching into it in a consumer is a sign the canonical schema is missing a
    field, which is a change to make here.
    """

    account: str
    source: str
    as_of: datetime
    base_currency: str
    positions: tuple[Position, ...] = ()
    balances: tuple[Balance, ...] = ()
    margin: Margin | None = None
    orders: tuple[Order, ...] = ()
    fills: tuple[Fill, ...] = ()
    vendor: Mapping[str, Any] = field(default_factory=dict)

    def balance(self, currency: str) -> Balance | None:
        for b in self.balances:
            if b.currency == currency:
                return b
        return None

    def by_kind(self, kind: str) -> tuple[Position, ...]:
        return tuple(p for p in self.positions if p.instrument.kind == kind)

    def net_liquidation(self) -> float | None:
        return self.margin.net_liquidation if self.margin else None
