"""live -- what a source says right now. Stamped, read-only, never stored.

The other half of the access axis from `history`. A history read is fixed:
given `as_of`, the same rows forever. A live read is a function of the moment
you ask, and says when that was. Neither writes to the store; keeping a live
answer is `history.record(snapshot)`, an explicit append through history's
one write path, never a side effect of reading.

Each asset class is a module or a read here, not a package of its own:
`live.fx` for rates, `live.option_chain()` for option quotes. The rows carry
the same identity columns as the matching history table (`antlia.schema`), so
a snapshot taken today joins onto yesterday's history without a mapping.

Importing this package needs nothing beyond the standard library; the query
engine and a broker SDK are imported only by the read that uses them.
"""

from __future__ import annotations

from antlia.live import fx
from antlia.live.base import LiveSource
from antlia.live.reads import option_chain
from antlia.live.registry import known, register, unregister
from antlia.live.types import Snapshot

__all__ = [
    "LiveSource",
    "Snapshot",
    "fx",
    "known",
    "option_chain",
    "register",
    "unregister",
]
