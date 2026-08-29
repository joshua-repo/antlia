"""The rate table on disk: a TTL, and a copy that outlives a failed fetch.

`account` deliberately never persists anything, because account state is stale
the moment it is read and a saved snapshot is a lie waiting to be believed.
Rates are the opposite kind of fact: a six-hour-old USD/GBP is still a usable
USD/GBP, and refetching one per render is the only slow part of a dashboard.
So this layer caches, and the cache does two jobs:

- **Skip the network** while the entry is younger than `TTL`.
- **Survive a total outage.** When every source fails, the last table on disk
  comes back flagged `stale=True` rather than nothing at all. A stale rate the
  consumer is told about beats a blank page; a stale rate it is *not* told
  about is the failure mode this whole layer is trying to avoid.

**The TTL is measured from the fetch, not from `RateTable.as_of`.** They are
different facts and both matter: `as_of` is when the rate was true, `fetched_at`
is when we asked. Yahoo's FX stamp is the last daily close, so it is already
twenty hours old by lunchtime -- a TTL keyed to it would expire instantly and
the cache would never once hit. `fetched_at` is therefore the cache's own
bookkeeping and stays out of `RateTable`, which is the consumer's contract.

It lives in `~/.antlia/` (or `$ANTLIA_HOME`), beside the credentials file, so
one directory holds everything antlia keeps on a machine. Nothing here is
secret and nothing here cannot be regenerated: deleting the file costs one
round-trip.

This is **not** caching on a consumer's behalf, which the boundary forbids. It
is this layer's own source table, and a consumer could not build it -- it has
no access to the fallback chain that produced it.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from antlia.auth.credentials import home
from antlia.fx.base import utc
from antlia.fx.types import RateTable

FILENAME = "fx.json"

#: How long a cached entry is used without asking anyone. FX moves less than
#: this matters for a position dashboard or a report, and a shorter window buys
#: precision no consumer of this layer has asked for.
TTL = timedelta(hours=6)

_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class Entry:
    """A cached table, plus when it was actually fetched."""

    table: RateTable
    fetched_at: datetime

    @property
    def age(self) -> timedelta:
        """Since the fetch. Not `table.age`, which is since the rate was true."""
        return datetime.now(UTC) - utc(self.fetched_at)


def path() -> Path:
    return home() / FILENAME


def read() -> Entry | None:
    """The last entry written, or None.

    A missing, unreadable or malformed cache is not an error worth surfacing --
    it just means there is nothing to reuse.
    """
    try:
        raw = json.loads(path().read_text())
        table = RateTable(
            base=raw["base"],
            rates={str(k): float(v) for k, v in raw["rates"].items()},
            as_of=datetime.fromisoformat(raw["as_of"]),
            source=raw["source"],
        )
        # A file written before `fetched_at` existed falls back to `as_of`,
        # which can only make the entry look older and trigger a refetch.
        fetched = raw.get("fetched_at") or raw["as_of"]
        return Entry(table=table, fetched_at=utc(datetime.fromisoformat(fetched)))
    except Exception:
        return None


def write(table: RateTable) -> None:
    """Replace the cached table, atomically.

    Written to a temporary file and renamed, because the alternative is a
    reader in another process seeing half a JSON document -- which `read()`
    would discard, turning a concurrent render into a needless refetch.
    """
    target = path()
    payload = {
        "base": table.base,
        "rates": dict(table.rates),
        "as_of": table.as_of.isoformat(),
        "source": table.source,
        "fetched_at": datetime.now(UTC).isoformat(),
    }
    try:
        with _lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix(f".{os.getpid()}.tmp")
            temp.write_text(json.dumps(payload))
            os.replace(temp, target)
    except OSError:
        # An unwritable cache costs a refetch next time. It is not a reason to
        # fail a call that has the rates in hand.
        pass


def clear() -> None:
    """Delete the cached table. Costs one round-trip, nothing else."""
    with _lock:
        path().unlink(missing_ok=True)


def covers(entry: Entry, currencies: Iterable[str]) -> bool:
    """Whether this entry can price every currency asked for."""
    return all(entry.table.has(c) for c in currencies)


def usable(entry: Entry, currencies: Iterable[str], ttl: timedelta = TTL) -> bool:
    """Whether this entry can be served without asking a source at all."""
    return covers(entry, currencies) and entry.age < ttl
