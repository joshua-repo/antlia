"""Recorded tables: live answers kept, served back through the store.

A recorded table is not fetched. `history.record(snapshot)` appends the raw
frame of a live answer, and this adapter exists so the store can read it back
with the projection of the live source that produced it -- the same map, so
the recording and the snapshot it came from cannot disagree.

This is the one place `history` reaches into `live`, and only inside
`projection()`: recording is history's write path calling in, never live
reaching out, and importing `antlia.history` still imports no part of `live`.
"""

from __future__ import annotations

from typing import Any

from antlia.history.base import HistorySource, Scope
from antlia.history.engine import pa
from antlia.history.errors import IngestFailed
from antlia.history.types import RECORDED, Window


class Recorded(HistorySource):
    """The history face of one live source. Reads only; never fetches."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.auth_source = None
        self.tables = RECORDED

    def scopes(self, table: str, symbol: str, window: Window, **options: Any) -> list[Scope]:
        return []

    def fetch(self, table: str, symbol: str, window: Window, scope: Scope) -> pa.Table | None:
        raise IngestFailed(
            self.name,
            f"{table} is recorded, not fetched: take a live snapshot and pass it to "
            "history.record()",
        )

    def verify(self) -> str:
        # Nothing remote stands behind a recorded table. Saying so beats a red
        # line in the doctor for a source that cannot fail this way.
        return "recorded tables only; nothing to call (see live.option_chain)"

    def projection(self, table: str) -> dict[str, str]:
        from antlia.live import registry as live_registry

        return live_registry.get(self.name).projection(table)


#: Registered by name in `history.registry`.
IBKR = Recorded("ibkr")
