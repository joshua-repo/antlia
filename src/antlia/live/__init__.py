"""live -- what a source says right now. Stamped, read-only, never stored.

The other half of the access axis from `history`. A history read is fixed:
given `as_of`, the same rows forever. A live read is a function of the moment
you ask, and says when that was. Neither writes to the store; recording a live
answer is an explicit append through `history`'s one write path, never a side
effect of reading it.

Each asset class is a module here, not a package of its own: `live.fx` today.
The rows a live read returns carry the same identity columns as the matching
history table (`antlia.schema`), so a snapshot taken today joins onto
yesterday's history without a mapping.
"""

from __future__ import annotations

from antlia.live import fx

__all__ = ["fx"]
