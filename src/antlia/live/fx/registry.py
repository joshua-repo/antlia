"""Source name -> rate adapter, resolved lazily, in the order they are tried.

Same lazy "module:attr" shape as `antlia.auth.registry`, for the same
reason: importing `antlia.live.fx` must not
drag in yfinance.

**The one difference is that order is meaningful here, so the listing is not
sorted.** In `auth` a caller names the source it wants; in `fx`
the layer picks, and which source answers first is the difference between a
market rate and a once-a-day fixing. `chain()` therefore returns insertion
order -- yfinance, then frankfurter -- and a registered source appends to the
end unless it replaces an existing name in place.

The listing is called `chain()` rather than `sources()` because `sources` is
also this package's adapter subpackage: importing an adapter binds the module
onto the package and would clobber a function of that name.
"""

from __future__ import annotations

import importlib
import threading

from antlia.auth.errors import UnknownSource
from antlia.live.fx.base import RateSource

#: Insertion order is the fallback order. yfinance is first because it is a
#: market rate that reconciles with the broker's own screen; Frankfurter is the
#: ECB's 16:00 CET fixing and can sit a day behind the market.
_BUILTIN: dict[str, str] = {
    "yfinance": "antlia.live.fx.sources.yfinance:YFinanceRates",
    "frankfurter": "antlia.live.fx.sources.frankfurter:FrankfurterRates",
}

_paths: dict[str, str] = dict(_BUILTIN)
_instances: dict[str, RateSource] = {}
_lock = threading.Lock()


def register(name: str, source: RateSource | str, *, first: bool = False) -> None:
    """Add or replace a rate source.

    `first=True` puts it at the head of the chain, which is how a caller says
    "prefer my source over the built-ins" -- the only reason a consumer would
    register one at all.
    """
    with _lock:
        _instances.pop(name, None)
        existing = name in _paths
        path = source if isinstance(source, str) else ""
        if first and not existing:
            reordered = {name: path, **_paths}
            _paths.clear()
            _paths.update(reordered)
        else:
            _paths[name] = path
        if not isinstance(source, str):
            _instances[name] = source


def unregister(name: str) -> None:
    with _lock:
        _paths.pop(name, None)
        _instances.pop(name, None)


def chain() -> list[str]:
    """Registered sources, in the order `rates()` will try them."""
    with _lock:
        return list(_paths)


def get(name: str) -> RateSource:
    """The adapter for `name`, importing its module on first use."""
    with _lock:
        cached = _instances.get(name)
        if cached is not None:
            return cached
        path = _paths.get(name)
        if not path:
            raise UnknownSource(name, _paths)

    module_name, _, attr = path.partition(":")
    obj = getattr(importlib.import_module(module_name), attr)
    source: RateSource = obj() if isinstance(obj, type) else obj

    with _lock:
        _instances[name] = source
    return source


def reset() -> None:
    """Restore the built-in chain. For tests."""
    with _lock:
        _paths.clear()
        _paths.update(_BUILTIN)
        _instances.clear()
