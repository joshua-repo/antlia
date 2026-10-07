"""Source name -> live adapter, resolved lazily.

Lazy for the reason every registry here is: importing `antlia.live` must not
import `ib_insync`. Named in the call, never chosen from a chain -- a live
answer is about one source at one moment, and silently answering from a
different one would hand back a different moment.
"""

from __future__ import annotations

import importlib
import threading

from antlia.auth.errors import UnknownSource
from antlia.live.base import LiveSource

_BUILTIN: dict[str, str] = {
    "ibkr": "antlia.live.sources.ibkr:IBKRLive",
}

_paths: dict[str, str] = dict(_BUILTIN)
_instances: dict[str, LiveSource] = {}
_lock = threading.Lock()


def register(name: str, source: LiveSource | str) -> None:
    """Add or replace a live source."""
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


def known() -> list[str]:
    """Every registered live source. Not `sources()`: that is the subpackage."""
    with _lock:
        return sorted(_paths)


def get(name: str) -> LiveSource:
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
    source: LiveSource = obj() if isinstance(obj, type) else obj
    with _lock:
        _instances[name] = source
    return source


def reset() -> None:
    """Restore the built-in registry. For tests."""
    with _lock:
        _paths.clear()
        _paths.update(_BUILTIN)
        _instances.clear()
