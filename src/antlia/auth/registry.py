"""Source name -> provider, resolved lazily.

The import of an adapter module is deferred until its source is actually asked
for. Without that, importing `antlia.auth` at all would drag in every vendor SDK
the user happens to have installed -- and fail for the ones they don't.
"""

from __future__ import annotations

import importlib
import threading

from antlia.auth.base import Provider
from antlia.auth.errors import UnknownSource

#: Built-in sources, as "module:attribute" so nothing imports at startup.
_BUILTIN: dict[str, str] = {
    "ibkr": "antlia.auth.providers.ibkr:IBKRProvider",
    "thetadata": "antlia.auth.providers.thetadata:ThetaDataProvider",
    "trading212": "antlia.auth.providers.trading212:Trading212Provider",
    "yfinance": "antlia.auth.providers.yfinance:YFinanceProvider",
}

_paths: dict[str, str] = dict(_BUILTIN)
_instances: dict[str, Provider] = {}
_lock = threading.Lock()


def register(name: str, provider: Provider | str) -> None:
    """Add or replace a source.

    `provider` may be an instance or a "module:attr" path, which keeps a
    third-party adapter as lazy as the built-in ones.
    """
    with _lock:
        _instances.pop(name, None)
        if isinstance(provider, str):
            _paths[name] = provider
        else:
            _paths[name] = ""
            _instances[name] = provider


def unregister(name: str) -> None:
    with _lock:
        _paths.pop(name, None)
        _instances.pop(name, None)


def sources() -> list[str]:
    """Every registered source name, whether or not its SDK is installed."""
    with _lock:
        return sorted(_paths)


def get(name: str) -> Provider:
    """The provider for `name`, importing its module on first use."""
    with _lock:
        cached = _instances.get(name)
        if cached is not None:
            return cached
        path = _paths.get(name)
        if not path:
            raise UnknownSource(name, _paths)

    module_name, _, attr = path.partition(":")
    obj = getattr(importlib.import_module(module_name), attr)
    provider: Provider = obj() if isinstance(obj, type) else obj

    with _lock:
        _instances[name] = provider
    return provider


def reset() -> None:
    """Restore the built-in registry. For tests."""
    with _lock:
        _paths.clear()
        _paths.update(_BUILTIN)
        _instances.clear()
