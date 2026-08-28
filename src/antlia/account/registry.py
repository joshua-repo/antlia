"""Source name -> account adapter, resolved lazily.

Same shape and the same reason as `antlia.auth.registry`: importing
`antlia.account` must not drag in every broker SDK installed on the machine.
"""

from __future__ import annotations

import importlib
import threading

from antlia.account.base import AccountSource
from antlia.auth.errors import UnknownSource

_BUILTIN: dict[str, str] = {
    "ibkr": "antlia.account.sources.ibkr:IBKRAccounts",
}

_paths: dict[str, str] = dict(_BUILTIN)
_instances: dict[str, AccountSource] = {}
_lock = threading.Lock()


def register(name: str, source: AccountSource | str) -> None:
    with _lock:
        _instances.pop(name, None)
        if isinstance(source, str):
            _paths[name] = source
        else:
            _paths[name] = ""
            _instances[name] = source


def unregister(name: str) -> None:
    with _lock:
        _paths.pop(name, None)
        _instances.pop(name, None)


def sources() -> list[str]:
    with _lock:
        return sorted(_paths)


def get(name: str) -> AccountSource:
    with _lock:
        cached = _instances.get(name)
        if cached is not None:
            return cached
        path = _paths.get(name)
        if not path:
            raise UnknownSource(name, _paths)

    module_name, _, attr = path.partition(":")
    obj = getattr(importlib.import_module(module_name), attr)
    source: AccountSource = obj() if isinstance(obj, type) else obj

    with _lock:
        _instances[name] = source
    return source


def reset() -> None:
    with _lock:
        _paths.clear()
        _paths.update(_BUILTIN)
        _instances.clear()
