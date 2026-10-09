"""What a live read hands back: a reading, stamped, with its raw kept beside it."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One live answer from one source.

    `table` is the canonical form, with the columns of `antlia.schema`'s
    dataset of the same name. `raw` is the vendor's frame as the adapter built
    it, unprojected: it is what `history.record()` keeps, so a recording is
    vendor-native in `raw/` like every other append and can be re-projected if
    the mapping turns out to be wrong.

    `received_at` is when the answer arrived. It is not the age of the data --
    each row's `stamp` is the source's own time for that quote, and the two
    differ whenever a quote is stale or delayed.
    """

    dataset: str
    source: str
    received_at: dt.datetime
    table: Any  # pyarrow.Table; Any so this module needs no pyarrow
    raw: Any

    @property
    def rows(self) -> int:
        return int(self.table.num_rows)

    def __str__(self) -> str:
        return f"{self.dataset} via {self.source}: {self.rows} rows at {self.received_at:%H:%M:%S}"
