"""Stand-ins shaped like ib_insync's objects.

Modelled on what a live gateway actually returned (a JPY+USD margin account,
44 positions, options among them), because the normalisation decisions here are
only interesting on data with those properties.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class Contract:
    symbol: str = "AAPL"
    secType: str = "STK"
    currency: str = "USD"
    exchange: str = ""
    primaryExchange: str = "NASDAQ"
    lastTradeDateOrContractMonth: str = ""
    strike: float = 0.0
    right: str = "0"
    multiplier: str = ""
    localSymbol: str = ""
    conId: int = 0
    tradingClass: str = ""


def option(symbol="AAOI", expiry="20260911", strike=100.0, right="P", conid=907051546):
    return Contract(
        symbol=symbol,
        secType="OPT",
        lastTradeDateOrContractMonth=expiry,
        strike=strike,
        right=right,
        multiplier="100",
        localSymbol=f"{symbol}  {expiry[2:]}{right}{int(strike * 1000):08d}",
        conId=conid,
        primaryExchange="AMEX",
    )


def stock(symbol="8058", currency="JPY", conid=13905888):
    return Contract(
        symbol=symbol,
        secType="STK",
        currency=currency,
        localSymbol=f"{symbol}.T",
        conId=conid,
        primaryExchange="TSEJ",
    )


@dataclass
class PortfolioItem:
    contract: Contract
    position: float
    marketPrice: float
    marketValue: float
    averageCost: float
    unrealizedPNL: float
    realizedPNL: float
    account: str = "U1"


@dataclass
class AccountValue:
    tag: str
    value: str
    currency: str = ""
    account: str = "U1"


@dataclass
class Execution:
    execId: str = "0000f8aa.6a91acdb"
    side: str = "BOT"
    shares: float = 1.0
    price: float = 6.26
    time: datetime = field(default_factory=lambda: datetime(2026, 8, 28, 14, 34, 37, tzinfo=UTC))
    acctNumber: str = "U1"
    orderId: int = 7


@dataclass
class CommissionReport:
    commission: float = 1.04028
    currency: str = "USD"
    realizedPNL: float = 782.88712


@dataclass
class FillRecord:
    contract: Contract
    execution: Execution = field(default_factory=Execution)
    commissionReport: CommissionReport = field(default_factory=CommissionReport)


@dataclass
class OrderRecord:
    orderId: int = 12
    action: str = "BUY"
    totalQuantity: float = 2.0
    orderType: str = "LMT"
    lmtPrice: float = 1.25
    auxPrice: float = 1.7976931348623157e308  # IBKR's "unset"
    tif: str = "DAY"
    account: str = "U1"
    permId: int = 99


@dataclass
class OrderStatus:
    status: str = "Submitted"
    filled: float = 0.0
    remaining: float = 2.0


@dataclass
class Trade:
    contract: Contract
    order: OrderRecord = field(default_factory=OrderRecord)
    orderStatus: OrderStatus = field(default_factory=OrderStatus)


class FakeIB:
    """Only the surface the adapter touches."""

    def __init__(self, values=(), portfolio=(), trades=(), fills=(), accounts=("U1",)):
        self._values = list(values)
        self._portfolio = list(portfolio)
        self._trades = list(trades)
        self._fills = list(fills)
        self._accounts = list(accounts)
        self.slept = 0.0
        self.client = type("C", (), {"serverVersion": lambda self: 176})()

    def managedAccounts(self):
        return self._accounts

    def accountValues(self, account: str = ""):
        return self._values

    def portfolio(self):
        return self._portfolio

    def openTrades(self):
        return self._trades

    def fills(self):
        return self._fills

    def sleep(self, seconds: float):
        self.slept += seconds

    def reqAccountUpdates(self, account: str = ""):  # pragma: no cover
        raise AssertionError("reqAccountUpdates blocks forever; the adapter must not call it")


def values_like_a_live_account() -> list[AccountValue]:
    """A JPY+USD margin account, as the gateway reported one."""
    return [
        AccountValue("Currency", "BASE", "BASE"),
        AccountValue("Currency", "JPY", "JPY"),
        AccountValue("Currency", "USD", "USD"),
        AccountValue("ExchangeRate", "1.00", "BASE"),
        AccountValue("ExchangeRate", "0.0062559", "JPY"),
        AccountValue("ExchangeRate", "1.00", "USD"),
        AccountValue("CashBalance", "-11386.5741", "BASE"),
        AccountValue("CashBalance", "-2304417.40", "JPY"),
        AccountValue("CashBalance", "3008.7613", "USD"),
        AccountValue("NetLiquidationByCurrency", "85032.0394", "BASE"),
        AccountValue("NetLiquidationByCurrency", "129672.6122", "JPY"),
        AccountValue("NetLiquidationByCurrency", "84221.9949", "USD"),
        AccountValue("UnrealizedPnL", "-1103.36", "BASE"),
        AccountValue("UnrealizedPnL", "142221.61", "JPY"),
        AccountValue("UnrealizedPnL", "-1991.79", "USD"),
        AccountValue("NetLiquidation", "85035.05", "USD"),
        AccountValue("MaintMarginReq", "57752.50", "USD"),
        AccountValue("InitMarginReq", "58502.00", "USD"),
        AccountValue("ExcessLiquidity", "33846.67", "USD"),
        AccountValue("AvailableFunds", "33020.23", "USD"),
        AccountValue("BuyingPower", "132080.92", "USD"),
        AccountValue("EquityWithLoanValue", "91522.23", "USD"),
        AccountValue("GrossPositionValue", "135972.33", "USD"),
        AccountValue("TotalCashValue", "-11386.57", "USD"),
        AccountValue("Cushion", "0.398032", ""),
        AccountValue("Leverage-S", "1.60", ""),
        AccountValue("DayTradesRemaining", "", ""),
        AccountValue("AccountType", "INDIVIDUAL", ""),
    ]
