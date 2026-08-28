"""Interactive Brokers account state, via TWS / IB Gateway.

Authentication, the socket and its lifetime belong to `antlia.auth`; this module
only maps. It is read-only by construction -- the session it borrows connects
with `readonly=True`, so the gateway itself refuses anything else.

Three things here were established against a live gateway and are easy to get
wrong from the docs alone:

- **`avgCost` includes the contract multiplier and `marketPrice` does not.** An
  option bought at 2.90 is reported as `avgCost=290.04` beside
  `marketPrice=3.50`. The canonical `average_price` divides the multiplier back
  out so the two are comparable; `Position.cost_basis` puts it back.
- **`reqAccountUpdates()` blocks forever** on a gateway that never sends
  `accountDownloadEnd`. ib_insync already subscribes on connect, so the adapter
  waits for the data to arrive instead of asking for it again.
- **A multi-currency account reports a `BASE` pseudo-currency** alongside the
  real ones. It is a consolidated rollup; summing over it double-counts.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any

from antlia import auth
from antlia.account.base import AccountSource, now, to_float
from antlia.account.types import (
    BASE,
    BOND,
    COMBO,
    CRYPTO,
    FOREX,
    FUND,
    FUTURE,
    FUTURE_OPTION,
    INDEX,
    OPTION,
    OTHER,
    STOCK,
    AccountSnapshot,
    Balance,
    Fill,
    Instrument,
    Margin,
    Order,
    Position,
)

SEC_TYPES = {
    "STK": STOCK,
    "OPT": OPTION,
    "FUT": FUTURE,
    "FOP": FUTURE_OPTION,
    "CASH": FOREX,
    "IND": INDEX,
    "FUND": FUND,
    "BOND": BOND,
    "CRYPTO": CRYPTO,
    "BAG": COMBO,
}

#: IBKR sends this for "no value" in double fields. It is not a price.
UNSET = 1e300

#: accountValues tag -> Margin field. The `-S` suffixed variants are the
#: security segment's own view and are deliberately not used: on a single-segment
#: account they duplicate these, and on a multi-segment one silently mixing the
#: two produces a number that is neither.
MARGIN_TAGS = {
    "NetLiquidation": "net_liquidation",
    "EquityWithLoanValue": "equity_with_loan",
    "GrossPositionValue": "gross_position_value",
    "TotalCashValue": "total_cash",
    "AccruedCash": "accrued_cash",
    "InitMarginReq": "initial_margin",
    "MaintMarginReq": "maintenance_margin",
    "AvailableFunds": "available_funds",
    "ExcessLiquidity": "excess_liquidity",
    "BuyingPower": "buying_power",
    "Cushion": "cushion",
    "Leverage-S": "leverage",
    "FullInitMarginReq": "full_initial_margin",
    "FullMaintMarginReq": "full_maintenance_margin",
    "LookAheadInitMarginReq": "look_ahead_initial_margin",
    "LookAheadMaintMarginReq": "look_ahead_maintenance_margin",
    "LookAheadExcessLiquidity": "look_ahead_excess_liquidity",
    "DayTradesRemaining": "day_trades_remaining",
}

BALANCE_TAGS = {
    "CashBalance": "cash",
    "SettledCash": "settled_cash",
    "AccruedCash": "accrued_interest",
    "NetLiquidationByCurrency": "net_liquidation",
    "UnrealizedPnL": "unrealized_pnl",
    "RealizedPnL": "realized_pnl",
    "ExchangeRate": "exchange_rate",
}


def sane(value: object) -> float | None:
    """Drop IBKR's sentinel and NaN, keeping real zeros."""
    number = to_float(value)
    if number is None or number != number or abs(number) >= UNSET:
        return None
    return number


def parse_expiry(raw: str | None) -> date | None:
    """`lastTradeDateOrContractMonth` is YYYYMMDD, or YYYYMM for a future."""
    if not raw:
        return None
    text = str(raw).strip()
    for fmt, pad in (("%Y%m%d", 8), ("%Y%m", 6)):
        if len(text) == pad:
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                return None
    return None


def instrument_of(contract: Any) -> Instrument:
    multiplier = sane(getattr(contract, "multiplier", None)) or 1.0
    right = (getattr(contract, "right", "") or "").strip().upper()
    strike = sane(getattr(contract, "strike", None))
    ids = {}
    if getattr(contract, "conId", None):
        ids["ibkr_conid"] = str(contract.conId)
    if getattr(contract, "localSymbol", None):
        ids["ibkr_local_symbol"] = str(contract.localSymbol)
    return Instrument(
        symbol=str(contract.symbol),
        kind=SEC_TYPES.get(getattr(contract, "secType", ""), OTHER),
        currency=str(getattr(contract, "currency", "") or ""),
        exchange=(getattr(contract, "primaryExchange", "") or getattr(contract, "exchange", ""))
        or None,
        expiry=parse_expiry(getattr(contract, "lastTradeDateOrContractMonth", None)),
        strike=strike or None,
        right=right if right in ("C", "P") else None,
        multiplier=multiplier,
        local_symbol=str(getattr(contract, "localSymbol", "") or "") or None,
        ids=ids,
    )


class IBKRAccounts(AccountSource):
    name = "ibkr"
    extra = "ibkr"
    package = "ib_insync"

    #: How long to wait for the gateway's initial account push.
    settle_timeout = 15.0

    def _session(self, profile: str | None, overrides: dict[str, Any]) -> Any:
        return auth.session("ibkr", profile, **overrides)

    def _settle(self, ib: Any) -> None:
        """Wait for the account snapshot ib_insync subscribed to on connect.

        Not `reqAccountUpdates()`: that call waits for an `accountDownloadEnd`
        the gateway may never send, and hangs the caller with no diagnostic.
        """
        deadline = self.settle_timeout
        while deadline > 0 and not ib.accountValues():
            ib.sleep(0.25)
            deadline -= 0.25

    def accounts(self, profile: str | None = None, **overrides: Any) -> list[str]:
        with self._session(profile, overrides) as ib:
            return [a for a in ib.managedAccounts() if a]

    def snapshot(
        self, account: str | None = None, profile: str | None = None, **overrides: Any
    ) -> AccountSnapshot:
        with self._session(profile, overrides) as ib:
            self._settle(ib)
            managed = [a for a in ib.managedAccounts() if a]
            account = account or (managed[0] if managed else "")
            values = [v for v in ib.accountValues() if not account or v.account == account]
            stamp = now()
            base = self._base_currency(values)
            return AccountSnapshot(
                account=account,
                source=self.name,
                as_of=stamp,
                base_currency=base,
                positions=self._positions(ib, account, stamp),
                balances=self._balances(values, account, stamp),
                margin=self._margin(values, account, base, stamp),
                orders=self._orders(ib, account, stamp),
                fills=self._fills(ib, account),
                vendor={
                    "managed_accounts": managed,
                    "server_version": ib.client.serverVersion(),
                    "account_values": {f"{v.tag}:{v.currency}": v.value for v in values},
                },
            )

    # -- mapping ----------------------------------------------------------

    def _base_currency(self, values: Sequence[Any]) -> str:
        """The currency the account's margin numbers are denominated in.

        Read off `NetLiquidation`, which IBKR reports once, in base currency.
        The `Currency` tag is *not* the answer: it appears once per currency
        held, so picking one gives whichever arrived first -- which on a
        JPY+USD account is a coin flip that silently mislabels every margin
        figure.
        """
        for v in values:
            if v.tag == "NetLiquidation" and v.currency not in ("", BASE):
                return str(v.currency)
        # Fallback: the currency whose rate to base is 1, by definition base.
        for v in values:
            if v.tag == "ExchangeRate" and v.currency != BASE and sane(v.value) == 1.0:
                return str(v.currency)
        return BASE

    def _pick(self, values: Iterable[Any], tag: str, currency: str | None = None) -> Any:
        """First value for a tag, preferring the account's own currency.

        Tags arrive several times over -- once per currency, plus `BASE`, plus
        an empty currency for the dimensionless ones (Cushion, Leverage). Taking
        whichever came first would make the result depend on wire order.
        """
        candidates = [v for v in values if v.tag == tag]
        if not candidates:
            return None
        for want in (currency, "", None):
            for v in candidates:
                if want is None and v.currency != BASE:
                    return v
                if want is not None and v.currency == want:
                    return v
        return candidates[0]

    def _positions(self, ib: Any, account: str, stamp: datetime) -> tuple[Position, ...]:
        out = []
        for item in ib.portfolio():
            if account and getattr(item, "account", account) != account:
                continue
            instrument = instrument_of(item.contract)
            avg = sane(item.averageCost)
            out.append(
                Position(
                    account=account,
                    source=self.name,
                    instrument=instrument,
                    quantity=float(item.position),
                    # avgCost is per contract *including* the multiplier;
                    # marketPrice is not. Put them on one scale.
                    average_price=(avg / instrument.multiplier if avg is not None else None),
                    market_price=sane(item.marketPrice),
                    market_value=sane(item.marketValue),
                    unrealized_pnl=sane(item.unrealizedPNL),
                    realized_pnl=sane(item.realizedPNL),
                    currency=instrument.currency,
                    as_of=stamp,
                )
            )
        return tuple(out)

    def _balances(
        self, values: Sequence[Any], account: str, stamp: datetime
    ) -> tuple[Balance, ...]:
        currencies = sorted({v.currency for v in values if v.currency})
        out = []
        for currency in currencies:
            fields = {
                field: sane(v.value)
                for tag, field in BALANCE_TAGS.items()
                if (v := self._pick([x for x in values if x.currency == currency], tag))
            }
            if not fields:
                continue
            out.append(
                Balance(account=account, source=self.name, currency=currency, as_of=stamp, **fields)
            )
        return tuple(out)

    def _margin(self, values: Sequence[Any], account: str, base: str, stamp: datetime) -> Margin:
        fields = {
            field: sane(v.value)
            for tag, field in MARGIN_TAGS.items()
            if (v := self._pick(values, tag, base))
        }
        return Margin(account=account, source=self.name, currency=base, as_of=stamp, **fields)

    def _orders(self, ib: Any, account: str, stamp: datetime) -> tuple[Order, ...]:
        out = []
        for trade in ib.openTrades():
            order, status = trade.order, trade.orderStatus
            if account and getattr(order, "account", "") not in ("", account):
                continue
            out.append(
                Order(
                    account=account,
                    source=self.name,
                    order_id=str(order.orderId),
                    instrument=instrument_of(trade.contract),
                    side="buy" if str(order.action).upper() == "BUY" else "sell",
                    quantity=float(order.totalQuantity),
                    filled=sane(status.filled) or 0.0,
                    remaining=sane(status.remaining),
                    order_type=str(order.orderType) or None,
                    limit_price=sane(order.lmtPrice),
                    stop_price=sane(order.auxPrice),
                    time_in_force=str(order.tif) or None,
                    status=str(status.status) or None,
                    as_of=stamp,
                    ids={"ibkr_perm_id": str(order.permId)} if order.permId else {},
                )
            )
        return tuple(out)

    def _fills(self, ib: Any, account: str) -> tuple[Fill, ...]:
        out = []
        for fill in ib.fills():
            execution, report = fill.execution, fill.commissionReport
            if account and execution.acctNumber and execution.acctNumber != account:
                continue
            out.append(
                Fill(
                    account=account,
                    source=self.name,
                    fill_id=str(execution.execId),
                    instrument=instrument_of(fill.contract),
                    side="buy" if str(execution.side).upper() in ("BOT", "BUY") else "sell",
                    quantity=float(execution.shares),
                    price=float(execution.price),
                    time=execution.time,
                    commission=sane(getattr(report, "commission", None)),
                    commission_currency=str(getattr(report, "currency", "") or "") or None,
                    realized_pnl=sane(getattr(report, "realizedPNL", None)),
                    ids={"ibkr_order_id": str(execution.orderId)},
                )
            )
        return tuple(out)
