"""`antlia.fx` is `antlia.live.fx`, under the name it shipped with.

FX moved under `live` when antlia was laid out as access (history, live) by
asset class: a spot rate table is a live read. This package re-exports the
public surface so `from antlia import fx` keeps working. New code should
import `antlia.live.fx`; submodules (`registry`, `cache`, ...) live there only.
"""

from __future__ import annotations

from antlia.live.fx import (
    CACHE_TTL,
    DEFAULT_CURRENCIES,
    PIVOT,
    RateSource,
    RatesUnavailable,
    RateTable,
    chain,
    clear_cache,
    rates,
    register,
    source,
    unregister,
)

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
