"""Trading212 account state, via the equity REST API.

Authentication, the client, its lifetime and its rate limiting belong to
`antlia.auth`; this module only maps. Endpoints and field names below were
**observed against a live account**, not taken from documentation.

Three things worth knowing before changing anything here:

- **Prices are in the instrument's currency, money is in the account's.** A GBP
  account holding a London line reports `averagePrice: 1875.0` in GBX (pence)
  beside `ppl: -43.0` in GBP, and `/equity/portfolio` carries no per-position
  currency. `quantity * currentPrice` is therefore off by 100x on some lines and
  right on others -- in one real account `FRXTl_EQ` quoted in GBP sat beside
  `GSKl_EQ` quoted in GBX, so the ticker suffix does not decide it either.
  `price_factor()` recovers the scale from two numbers the vendor already
  reported (see there), and anything it cannot pin down stays `None`.
- **`/equity/orders` (working orders) needs the trading scope, which this key
  deliberately does not have** and so answers 403. Withholding that scope is the
  right posture for antlia -- the layer has no execution path, and a key that
  cannot trade makes that a broker-enforced fact rather than a convention. The
  403 therefore degrades to no orders, recorded in `vendor`, instead of failing
  the whole snapshot.
- **Fills are real and come from `/equity/history/orders`.** Each item carries a
  `fill` sub-object with quantity, price, `filledAt` and a `walletImpact` that
  itemises taxes. An earlier note claiming T212 exposes no execution data was
  wrong.

`/equity/account/summary` is preferred over `/equity/account/info`: it returns
the account id and currency *and* the totals, at one call per 5s instead of
`info`'s one per 30s.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from antlia import auth
from antlia.account.base import AccountSource, now, to_float
from antlia.account.types import (
    STOCK,
    AccountSnapshot,
    Balance,
    Fill,
    Instrument,
    Order,
    Position,
)
from antlia.auth.errors import ConnectionFailed

SUMMARY = "/api/v0/equity/account/summary"
PORTFOLIO = "/api/v0/equity/portfolio"
ORDERS = "/api/v0/equity/orders"
HISTORY_ORDERS = "/api/v0/equity/history/orders"

#: How many historical orders to read for fills. The endpoint allows six calls
#: a minute, so one page is what a snapshot can afford.
FILL_PAGE = 50


def parse_time(raw: object) -> datetime | None:
    """T212 sends ISO 8601 with an offset, e.g. `2026-06-23T13:12:09.000Z`."""
    if not raw:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


#: Scale factors worth believing: a currency is quoted either in its major
#: unit or in a minor one that is a power of ten below it (GBP/GBX, ILS/ILA).
#: A factor that is not one of these is a signal the derivation is wrong.
PLAUSIBLE_FACTORS = (1.0, 0.01, 0.001, 100.0, 1000.0)

#: How far a derived factor may sit from a plausible one, relatively.
FACTOR_TOLERANCE = 0.02


def price_factor(
    quantity: float | None, average: float | None, current: float | None, ppl: float | None
) -> float | None:
    """What to multiply a price by to reach the account's currency.

    `/equity/portfolio` states prices in the instrument's currency and `ppl` in
    the account's. Their ratio is therefore the scale between the two, and it is
    derived rather than assumed:

        ppl = quantity * (current - average) * factor

    Returns None whenever that cannot be pinned down -- a position whose price
    has not moved makes the ratio undefined, and a ratio that is not close to a
    plausible power of ten means something other than unit scaling is in play
    (an FX component, a corporate action). Failing closed is the point: a
    portfolio value silently 100x wrong is worse than one that is absent.
    """
    if not quantity or average is None or current is None or ppl is None:
        return None
    native = quantity * (current - average)
    if abs(native) < 1e-9:
        return None
    factor = ppl / native
    for candidate in PLAUSIBLE_FACTORS:
        if abs(factor - candidate) <= FACTOR_TOLERANCE * candidate:
            return candidate
    return None


def instrument_of(ticker: str, meta: Any = None) -> Instrument:
    """T212 tickers look like `AAPL_US_EQ`, `GSKl_EQ`, `SHELl_EQ`.

    The symbol is the part before the first underscore, which for London lines
    keeps a trailing venue letter (`GSKl`, not `GSK`). That is left alone
    deliberately: guessing it off would silently merge two listings, and
    cross-source identity is an open question. The untouched ticker and the
    ISIN, where the payload carries one, go into `ids` so a real resolver has
    something to work with.
    """
    meta = meta or {}
    ids = {}
    if ticker:
        ids["trading212_ticker"] = ticker
    if meta.get("isin"):
        ids["isin"] = str(meta["isin"])
    return Instrument(
        symbol=ticker.split("_", 1)[0] if ticker else ticker,
        kind=STOCK,
        currency=str(meta.get("currency") or ""),
        local_symbol=ticker or None,
        ids=ids,
    )


class Trading212Accounts(AccountSource):
    name = "trading212"
    extra = "trading212"
    package = "httpx"

    def _session(self, profile: str | None, overrides: dict[str, Any]) -> Any:
        return auth.session("trading212", profile, **overrides)

    def _get(self, client: Any, path: str, params: dict[str, Any] | None = None) -> Any:
        """One GET, with the refusals translated into what to do about them."""
        response = client.get(path, params=params)
        status = response.status_code
        if status == 401:
            raise ConnectionFailed(
                "trading212", f"401 on {path} -- the key/secret pair is not valid"
            )
        if status == 403:
            raise ConnectionFailed(
                "trading212", f"403 on {path} -- the key lacks the scope for this endpoint"
            )
        if status == 429:
            reset = response.headers.get("x-ratelimit-reset", "?")
            raise ConnectionFailed(
                "trading212",
                f"429 on {path} -- this endpoint's budget is "
                f"{response.headers.get('x-ratelimit-limit', '?')} per "
                f"{response.headers.get('x-ratelimit-period', '?')}s, resets at {reset}",
            )
        if status >= 400:
            raise ConnectionFailed("trading212", f"HTTP {status} on {path}: {response.text[:200]}")
        return response.json()

    def _try(self, client: Any, path: str, params: dict[str, Any] | None = None) -> tuple[Any, str]:
        """A GET whose 403 is a missing scope rather than a broken snapshot."""
        try:
            return self._get(client, path, params), ""
        except ConnectionFailed as exc:
            if "403" in str(exc):
                return None, f"{path}: not permitted by this key"
            raise

    def accounts(self, profile: str | None = None, **overrides: Any) -> list[str]:
        with self._session(profile, overrides) as client:
            summary = self._get(client, SUMMARY)
            account_id = summary.get("id")
            return [str(account_id)] if account_id is not None else []

    def snapshot(
        self, account: str | None = None, profile: str | None = None, **overrides: Any
    ) -> AccountSnapshot:
        with self._session(profile, overrides) as client:
            summary = self._get(client, SUMMARY)
            portfolio = self._get(client, PORTFOLIO)
            orders, orders_note = self._try(client, ORDERS)
            history, history_note = self._try(client, HISTORY_ORDERS, {"limit": FILL_PAGE})

            stamp = now()
            currency = str(summary.get("currency") or "")
            resolved = account or str(summary.get("id", ""))
            unavailable = [note for note in (orders_note, history_note) if note]
            return AccountSnapshot(
                account=resolved,
                source=self.name,
                as_of=stamp,
                base_currency=currency,
                positions=self._positions(portfolio, resolved, currency, stamp),
                balances=(self._balance(summary, resolved, currency, stamp),),
                # Cash/ISA product: there is no margin for this API to report.
                margin=None,
                orders=self._orders(orders, resolved, stamp),
                fills=self._fills(history, resolved),
                vendor={"summary": summary, "unavailable": unavailable},
            )

    # -- mapping ----------------------------------------------------------

    def _positions(
        self, portfolio: Any, account: str, currency: str, stamp: datetime
    ) -> tuple[Position, ...]:
        out = []
        for item in portfolio or ():
            ticker = str(item.get("ticker", ""))
            quantity = to_float(item.get("quantity")) or 0.0
            average = to_float(item.get("averagePrice"))
            current = to_float(item.get("currentPrice"))
            ppl = to_float(item.get("ppl"))
            factor = price_factor(quantity, average, current, ppl)
            out.append(
                Position(
                    account=account,
                    source=self.name,
                    instrument=instrument_of(ticker),
                    quantity=quantity,
                    # Instrument-native units -- GBX on some London lines.
                    average_price=average,
                    market_price=current,
                    market_value=(
                        quantity * current * factor
                        if factor is not None and current is not None
                        else None
                    ),
                    # `ppl` is the one money field the payload already states in
                    # the account's own currency.
                    unrealized_pnl=ppl,
                    realized_pnl=None,
                    currency=currency,
                    as_of=stamp,
                )
            )
        return tuple(out)

    def _balance(self, summary: Any, account: str, currency: str, stamp: datetime) -> Balance:
        cash = summary.get("cash") or {}
        investments = summary.get("investments") or {}
        return Balance(
            account=account,
            source=self.name,
            currency=currency,
            cash=to_float(cash.get("availableToTrade")),
            settled_cash=None,
            accrued_interest=None,
            net_liquidation=to_float(summary.get("totalValue")),
            unrealized_pnl=to_float(investments.get("unrealizedProfitLoss")),
            realized_pnl=to_float(investments.get("realizedProfitLoss")),
            exchange_rate=None,
            as_of=stamp,
        )

    def _orders(self, orders: Any, account: str, stamp: datetime) -> tuple[Order, ...]:
        out = []
        for item in orders or ():
            quantity = to_float(item.get("quantity")) or 0.0
            side = str(item.get("side") or "").upper()
            order_id = item.get("id")
            out.append(
                Order(
                    account=account,
                    source=self.name,
                    order_id=str(order_id) if order_id is not None else "",
                    instrument=instrument_of(str(item.get("ticker", ""))),
                    side="sell" if side == "SELL" or quantity < 0 else "buy",
                    quantity=abs(quantity),
                    filled=to_float(item.get("filledQuantity")) or 0.0,
                    remaining=None,
                    order_type=str(item.get("type") or "") or None,
                    limit_price=to_float(item.get("limitPrice")),
                    stop_price=to_float(item.get("stopPrice")),
                    time_in_force=str(item.get("timeValidity") or "") or None,
                    status=str(item.get("status") or "") or None,
                    as_of=stamp,
                    ids={"trading212_order_id": str(order_id)} if order_id is not None else {},
                )
            )
        return tuple(out)

    def _fills(self, history: Any, account: str) -> tuple[Fill, ...]:
        out = []
        for item in (history or {}).get("items", ()):
            order, fill = item.get("order") or {}, item.get("fill") or {}
            if not fill:
                continue
            filled_at = parse_time(fill.get("filledAt") or order.get("createdAt"))
            price = to_float(fill.get("price"))
            if filled_at is None or price is None:
                continue
            impact = fill.get("walletImpact") or {}
            # Taxes arrive as signed wallet debits; a commission is a cost.
            taxes = abs(sum(to_float(t.get("quantity")) or 0.0 for t in impact.get("taxes") or ()))
            fill_id = fill.get("id")
            out.append(
                Fill(
                    account=account,
                    source=self.name,
                    fill_id=str(fill_id) if fill_id is not None else "",
                    instrument=instrument_of(str(order.get("ticker", "")), order.get("instrument")),
                    side="sell" if str(order.get("side", "")).upper() == "SELL" else "buy",
                    quantity=abs(to_float(fill.get("quantity")) or 0.0),
                    price=price,
                    time=filled_at,
                    commission=taxes or None,
                    commission_currency=str(impact.get("currency") or "") or None,
                    realized_pnl=None,
                    ids={"trading212_order_id": str(order.get("id"))} if order.get("id") else {},
                )
            )
        return tuple(out)
