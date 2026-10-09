"""Source name -> history adapter, resolved lazily, in preference order.

Same lazy "module:attr" shape as the other registries: importing
`antlia.history` must not drag in a vendor SDK.

**The listing is `chain()`, not `sources()`** -- `sources` is also this
package's adapter subpackage, and importing an adapter binds the module over a
function of that name. `fx` avoided that collision; this follows `fx`.

Order is insertion order and is the **default preference**, which is as much of
open question 2 (*source precedence*) as is honestly settled: with one source
registered there is nothing to arbitrate, and the placeholder rule is the one
`fx` already holds -- **one source answers a read, never two stitched
together.** When a second source arrives, precedence gets designed properly;
until then a caller naming `source=` gets exactly that source, and a caller
naming none gets the first in the chain.
"""

from __future__ import annotations

import importlib
import threading
from pathlib import Path

from antlia.auth.errors import UnknownSource
from antlia.history.base import HistorySource
from antlia.history.store import root

_BUILTIN: dict[str, str] = {
    "thetadata": "antlia.history.sources.thetadata:ThetaDataHistory",
    "fred": "antlia.history.sources.fred:FredHistory",
    "ibkr": "antlia.history.sources.recorded:IBKR",
}

_paths: dict[str, str] = dict(_BUILTIN)
_instances: dict[str, HistorySource] = {}
_lock = threading.Lock()


def register(name: str, source: HistorySource | str, *, first: bool = False) -> None:
    """Add or replace a history source. `first=True` makes it the default."""
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
    """Registered sources, in preference order. The first is the default."""
    with _lock:
        return list(_paths)


def default(table: str | None = None) -> str:
    """The first source in the chain -- or the first that serves `table`.

    Sources serve different tables (ThetaData the market tables, FRED the rate
    series), so "the default" is a per-table answer. Asking reads each
    adapter's `tables`, which imports the adapter module but never its SDK.
    """
    with _lock:
        if not _paths:
            raise UnknownSource("", _paths)
        names = list(_paths)
    if table is None:
        return names[0]
    for name in names:
        if table in get(name).tables:
            return name
    raise UnknownSource(f"(none serves {table})", names)


def source(name: str | None = None) -> HistorySource:
    """The adapter for `name`, or the first in the chain. Imported on first use.

    `get` demands a name; this is the form a consumer wants, where naming none
    means "whichever answers by default".
    """
    return get(name or default())


def bind(
    source: str | None, store: str | Path | None, table: str | None = None
) -> tuple[Path, HistorySource]:
    """The store root and the adapter a call is about.

    Every entry point needs both and resolves them the same way, so "which
    source answers when you name none" and "where the store is" are each
    decided in one place. It lives here rather than beside the Parquet tier
    because resolving a name is this module's job; the tier below must not
    know that adapters exist.
    """
    return root(store), get(source or default(table))


def get(name: str) -> HistorySource:
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
    source: HistorySource = obj() if isinstance(obj, type) else obj

    with _lock:
        _instances[name] = source
    return source


def reset() -> None:
    """Restore the built-in chain. For tests."""
    with _lock:
        _paths.clear()
        _paths.update(_BUILTIN)
        _instances.clear()
