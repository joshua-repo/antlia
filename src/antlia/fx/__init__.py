"""Foreign exchange rates, so figures in different currencies can be added up.

    from antlia import fx

    table = fx.rates(("USD", "GBP", "JPY", "HKD"))
    table.convert(83289.64, "GBP", "USD")   # -> 112753.17
    table.source, table.as_of, table.stale

FX is market data, and antlia is the single source of truth for market data.
It lives here rather than in a dashboard because every other consumer -- a
backtest, a risk report -- needs the same rates, and a provider chain owned by
one visualisation layer is unreachable from all of them and gets reimplemented
slightly differently in each.

**Two sources, tried in order, because neither is good enough alone.**
`yfinance` first: a market rate that reconciles with the broker's own screen.
`frankfurter` (the ECB's 16:00 CET fixing, keyless) underneath it, because a
chain whose only link is an unofficial scraper is not a chain. Each module says
in its docstring why it sits where it does.

Four rules this layer is built around:

- **USD is the pivot.** Both sources answer USD-based natively, so every
  conversion crosses through it and adding a currency is a one-line change.
- **Never invent a rate.** An unknown currency converts to `None`, never to an
  approximation. A wrong rate mis-states a whole portfolio quietly; an absent
  one just removes a feature.
- **Cache with a TTL, and keep the stale copy.** Rates are cacheable in a way
  account state deliberately is not. When every source fails, the last table
  comes back with `stale=True` -- the consumer decides whether that is good
  enough, but it is told.
- **Raise, don't return None.** `rates()` raises `RatesUnavailable` when it has
  nothing at all, not even a stale table. Degrading to "no rates" is the
  consumer's policy, not this layer's. It subclasses `auth`'s `ConnectionFailed`,
  so a consumer catching that -- or `AuthError` -- needs no new import.

## A broker's rate is not an FX source -- decided 2026-08-29

IBKR reports its own rate per currency on each account balance,
live, and by definition the one its account totals were computed with. That
number stays an account concern and is **not** reachable through `fx`:

1. It is not a market rate. It is what *one* broker used to value *one* account
   at *one* instant, and it means something only against that account's totals.
2. It cannot answer this layer's question. `rates()` takes currencies; a broker
   rate needs a credential, a connection and an account id. Admitting one would
   force an `account=` argument or a silent "first account" -- and then the same
   call returns different numbers depending on configuration, which is exactly
   the failure "single source of truth" is meant to prevent.
3. The two genuinely differ: Yahoo said USD/JPY 160.04 where IBKR said 160.08.
   One call with two answers is worse than one answer and a documented way to
   compare the other.

So the rule is: **restating a broker's own account totals uses that broker's
own rate; combining across brokers, or converting anything that is not an
account, uses `fx`.** To compare them, `RateTable.inverse()` is deliberately in
the broker's orientation (units of base per 1 unit of the currency, which is
how IBKR reports it):

    table.inverse("JPY")                        # 0.006248  -- the market
    # 0.006247 -- IBKR's own, as it reports it

Adding a source is a `RateSource` with a `rates()` and a `register()`.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

from antlia.auth.errors import AuthError
from antlia.fx import cache, registry
from antlia.fx.base import RateSource
from antlia.fx.cache import TTL as CACHE_TTL
from antlia.fx.errors import RatesUnavailable
from antlia.fx.registry import chain, register, unregister
from antlia.fx.types import DEFAULT_CURRENCIES, PIVOT, RateTable


def source(name: str) -> RateSource:
    """The adapter for `name`, imported on first use."""
    return registry.get(name)


def rates(
    currencies: tuple[str, ...] = DEFAULT_CURRENCIES,
    *,
    source: str | None = None,
    use_cache: bool = True,
    ttl: timedelta = CACHE_TTL,
) -> RateTable:
    """The best rate table available for `currencies`, USD-based.

    Order: a fresh cached table, then each source in the chain in turn, then a
    stale cached table flagged as such. `RatesUnavailable` (a `ConnectionFailed`)
    only when there is nothing at all -- a consumer that would rather see no
    rates than an error catches it and decides that for itself.

    `source` pins one adapter instead of walking the chain, and then only a
    cached table from that same source will be reused. `use_cache=False`
    forces a live fetch (the result is still cached, so the next caller
    benefits). `ttl` overrides how long a cached table counts as fresh.
    """
    needed = tuple(dict.fromkeys((PIVOT, *currencies)))
    names = [source] if source else registry.chain()

    cached = cache.read()
    if cached is not None and source is not None and cached.table.source != source:
        # A table pinned to one source must not be answered from another's.
        cached = None
    if use_cache and cached is not None and cache.usable(cached, needed, ttl):
        return cached.table

    tried: list[str] = []
    for name in names:
        try:
            table = registry.get(name).rates(needed)
        except AuthError as exc:
            # Already names its own source -- prefixing would print it twice.
            tried.append(str(exc))
            continue
        except Exception as exc:
            # A bug or a raw vendor error in one adapter must not end the
            # chain -- that is the whole point of having a second source. It
            # is recorded, and surfaces in the message if nothing else works.
            tried.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        cache.write(table)
        return table

    if cached is not None and cache.covers(cached, needed):
        return dataclasses.replace(cached.table, stale=True)

    detail = "; ".join(tried) or "no sources registered"
    raise RatesUnavailable("fx", f"no source could price {', '.join(needed)}; {detail}")


def clear_cache() -> None:
    """Drop the cached table. Costs one round-trip, nothing else."""
    cache.clear()


__all__ = [
    "rates",
    "source",
    "chain",
    "register",
    "unregister",
    "clear_cache",
    "RateTable",
    "RateSource",
    "RatesUnavailable",
    "PIVOT",
    "DEFAULT_CURRENCIES",
    "CACHE_TTL",
]
