"""antlia -- the data layer.

An air pump moves what is in there out to here without altering it. Antlia pulls
market data out of vendor APIs and makes it available in one shape,
and resists improving anything on the way through.

Laid out as access by asset class: `antlia.history` (fixed, cached) and
`antlia.live` (right now, never stored), over the asset classes declared in
`antlia.schema`. `antlia.auth` and `antlia.gateway` are the infrastructure
beneath them, and `antlia.fx` is the original name for `antlia.live.fx`.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["auth", "fx", "gateway", "history", "live", "schema", "__version__"]


def __getattr__(name: str) -> object:
    """Import submodules on first attribute access.

    `import antlia` must stay free of vendor SDKs and of the query engine, so
    the subpackages are not imported here.
    """
    if name in {"auth", "fx", "gateway", "history", "live", "schema"}:
        import importlib

        return importlib.import_module(f"antlia.{name}")
    raise AttributeError(f"module 'antlia' has no attribute {name!r}")
