"""The live-source contract. The same seam as `history`, minus the planning.

A source supplies:

1. **What it can serve** -- `datasets`, names from `antlia.schema`.
2. **How to fetch one answer** -- `fetch()`, returning the vendor's frame as
   the adapter tabulated it: vendor field names, vendor units, vendor
   sentinels. Request parameters may be appended (which moment, which
   symbol); nothing the vendor sent may be changed.
3. **How that frame maps onto the dataset** -- `projection()`, a
   `{canonical column: SQL expression}` map, exactly as in `history`. The live
   read applies it to the fresh frame; `history` applies the same map to a
   recorded one, which is what keeps a recording and the live answer it came
   from identical.

A source authenticates through `antlia.auth` and never around it.
"""

from __future__ import annotations

import abc
import importlib
from types import ModuleType
from typing import Any

from antlia.auth.errors import MissingExtra


class LiveSource(abc.ABC):
    """Live data from one vendor."""

    #: Name in the registry, in a Snapshot's `source`, and in `raw/<source>/`
    #: once recorded. Changing it orphans every recording.
    name: str
    #: Source name in `antlia.auth`.
    auth_source: str | None = None
    #: pip extra and import name, for a readable failure when the SDK is absent.
    extra: str | None = None
    package: str | None = None
    #: Datasets this source can answer.
    datasets: frozenset[str] = frozenset()

    @abc.abstractmethod
    def fetch(self, dataset: str, symbol: str, **options: Any) -> Any:
        """One live answer, vendor-native, as a pyarrow.Table. None for no rows."""

    @abc.abstractmethod
    def projection(self, dataset: str) -> dict[str, str]:
        """`{canonical column: SQL expression}` over this source's raw columns."""

    def require(self, module: str) -> ModuleType:
        try:
            return importlib.import_module(module)
        except ImportError as exc:
            raise MissingExtra(self.name, self.extra or self.name, self.package or module) from exc
