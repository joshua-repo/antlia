"""Source name -> gateway adapter, resolved lazily.

Same "module:attr" shape as the other registries, for the same reason: naming
a source must not import anything a caller did not ask for.

**The difference here is that "no adapter" is a legitimate answer.** Most
sources are REST APIs with nothing behind them to look at, so `get()` returns
`None` for them rather than raising -- a consumer asks about every source it
knows and draws a screen control only where there is a screen.

That makes a typo dangerous: `describe("ibkrr")` returning `None` would be read
as "this source has no gateway" and the mistake would never surface. So the two
cases are separated by asking `auth`'s registry: a name it knows has simply no
gateway, and a name nobody knows is `UnknownSource`. That check costs a dict
lookup and imports nothing -- `auth.registry.sources()` does not load adapters.

The listing is `gateways()`, not `sources()`, because `sources` is also this
package's adapter subpackage: importing an adapter binds the module onto the
package and would clobber a function of that name. `fx`
sidesteps it with `chain()`; here the honest name happens to be the safe one.
"""

from __future__ import annotations

import importlib
import threading

from antlia.auth import registry as auth_registry
from antlia.auth.errors import UnknownSource
from antlia.gateway.base import Gateway

#: Built-in gateways, as "module:attribute" so nothing imports at startup.
_BUILTIN: dict[str, str] = {
    "ibkr": "antlia.gateway.sources.ibkr:IBKRGateway",
}

_paths: dict[str, str] = dict(_BUILTIN)
_instances: dict[str, Gateway] = {}
_lock = threading.Lock()


def register(name: str, gateway: Gateway | str) -> None:
    """Add or replace a gateway. An instance or a lazy "module:attr" path."""
    with _lock:
        _instances.pop(name, None)
        if isinstance(gateway, str):
            _paths[name] = gateway
        else:
            _paths[name] = ""
            _instances[name] = gateway


def unregister(name: str) -> None:
    with _lock:
        _paths.pop(name, None)
        _instances.pop(name, None)


def gateways() -> list[str]:
    """Every source that has a gateway, whether or not it is reachable."""
    with _lock:
        return sorted(_paths)


def get(name: str) -> Gateway | None:
    """The gateway for `name`, or `None` if that source has none.

    Raises `UnknownSource` for a name neither this registry nor `auth`'s knows,
    so a misspelling is a failure rather than a silent "no gateway".
    """
    with _lock:
        cached = _instances.get(name)
        if cached is not None:
            return cached
        path = _paths.get(name)
        if path is None:
            known = set(_paths) | set(auth_registry.sources())
            if name not in known:
                raise UnknownSource(name, known)
            return None

    module_name, _, attr = path.partition(":")
    obj = getattr(importlib.import_module(module_name), attr)
    gateway: Gateway = obj() if isinstance(obj, type) else obj

    with _lock:
        _instances[name] = gateway
    return gateway


def reset() -> None:
    """Restore the built-in registry. For tests."""
    with _lock:
        _paths.clear()
        _paths.update(_BUILTIN)
        _instances.clear()
