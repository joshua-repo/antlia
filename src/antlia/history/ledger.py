"""What was asked for, as opposed to what came back.

The store alone cannot answer *"is this window covered?"*. A date with no rows
is ambiguous: it might be a market holiday, an expiration that had not been
listed yet, or a fetch that never happened. Only the first two are answers.
Without somewhere to write down "we asked, and there was nothing", a cache-first
reader re-fetches every holiday in the range, forever -- which on a metered free
plan is the difference between a warm cache costing nothing and costing the
whole daily budget.

So each unit of work records one row here: which source, table, symbol and
scope, the window asked for, how many rows arrived, and how it went.

**Three statuses, and the third is why this file is not a boolean.**

- `ok` -- rows arrived.
- `empty` -- the vendor answered and had nothing. Covered. Never ask again.
- `denied` -- the vendor refused and will keep refusing: a plan boundary, a
  window before its history begins. **Not** covered, and also not missing;
  retrying it is pointless. Collapsing this into either of the others gives you
  a planner that either loops on a permanent refusal or silently claims to hold
  data it was never allowed to read.

Append-only Parquet, one small file per ingest run, read back through the same
DuckDB the store uses. It is rebuildable in the sense that matters -- deleting
it costs re-fetches, not data -- but it is not derived from `raw/`, because the
fact it records (that nothing existed) leaves no trace there.
"""

from __future__ import annotations

import datetime as dt
import itertools
import os
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from antlia.history.engine import pa, pq
from antlia.history.store import connect, ledger_dir
from antlia.history.types import Window, merge

OK = "ok"
EMPTY = "empty"
DENIED = "denied"

#: Statuses that mean "this window has been settled; do not ask again".
SETTLED = (OK, EMPTY)

SCHEMA = pa.schema(
    [
        ("source", pa.string()),
        ("table", pa.string()),
        ("symbol", pa.string()),
        ("scope", pa.string()),
        ("start", pa.date32()),
        ("end", pa.date32()),
        ("rows", pa.int64()),
        ("status", pa.string()),
        ("ingested_at", pa.timestamp("us", tz="UTC")),
        ("detail", pa.string()),
    ]
)

_lock = threading.Lock()

#: Distinguishes two flushes that land in the same microsecond, which a
#: parallel ingest makes ordinary rather than theoretical.
_serial = itertools.count()


@dataclass(frozen=True, slots=True)
class Entry:
    """One unit of work, and how it went."""

    source: str
    table: str
    symbol: str
    scope: str
    window: Window
    rows: int
    status: str
    detail: str = ""

    def __str__(self) -> str:
        where = f"{self.symbol}" + (f"/{self.scope}" if self.scope else "")
        return f"{self.status:6} {where} {self.window} ({self.rows} rows)"


def record(base: Path, entries: Sequence[Entry], *, at: dt.datetime | None = None) -> int:
    """Append entries. One file per call, so a run is never half-written."""
    if not entries:
        return 0
    stamp = at or dt.datetime.now(dt.UTC)
    frame = pa.Table.from_pydict(
        {
            "source": [e.source for e in entries],
            "table": [e.table for e in entries],
            "symbol": [e.symbol for e in entries],
            "scope": [e.scope for e in entries],
            "start": [e.window.start for e in entries],
            "end": [e.window.end for e in entries],
            "rows": [e.rows for e in entries],
            "status": [e.status for e in entries],
            "ingested_at": [stamp] * len(entries),
            "detail": [e.detail for e in entries],
        },
        schema=SCHEMA,
    )
    directory = ledger_dir(base)
    with _lock:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{stamp:%Y%m%dT%H%M%S%f}-{os.getpid()}-{next(_serial)}.parquet"
        # Written beside the target and renamed, for the same reason
        # `store.write` stages: a killed process must not leave a truncated
        # file in the read glob, where it would make the whole ledger
        # unreadable rather than costing one run's bookkeeping.
        temp = target.with_suffix(".tmp")
        pq.write_table(frame, temp)
        os.replace(temp, target)
    return len(entries)


def _query(base: Path, sql: str, params: Sequence[object]) -> list[tuple[Any, ...]]:
    """One read of the ledger, or `[]` when there is no ledger yet.

    Every reader here wants the same three things -- is there a file, open a
    connection, close it again -- and an empty ledger is not an error worth
    surfacing: it means nothing has been asked for. `?` in `sql` binds the
    glob, so a caller writes only its own predicates.
    """
    directory = ledger_dir(base)
    if not directory.exists() or not any(directory.glob("*.parquet")):
        return []
    con = connect()
    try:
        return con.execute(sql, [str(directory / "*.parquet"), *params]).fetchall()
    finally:
        con.close()


def windows(
    base: Path,
    source: str,
    table: str,
    symbol: str,
    scope: str | None = None,
    *,
    statuses: Iterable[str] = SETTLED,
) -> list[Window]:
    """Settled windows for one scope, merged into the fewest that cover them.

    `scope=None` means every scope, which is what an equity table wants and
    what an option-level summary wants; a per-expiration plan passes the
    expiration.
    """
    where = ["source = ?", '"table" = ?', "symbol = ?"]
    params: list[object] = [source, table, symbol]
    if scope is not None:
        where.append("scope = ?")
        params.append(scope)
    marks = ", ".join("?" for _ in statuses)
    where.append(f"status IN ({marks})")
    params.extend(statuses)
    rows = _query(
        base,
        f'SELECT "start", "end" FROM read_parquet(?) WHERE {" AND ".join(where)}',
        params,
    )
    return merge(Window(start, end) for start, end in rows)


def denied(
    base: Path, source: str, table: str, symbol: str, scope: str | None = None
) -> list[Window]:
    """Windows the source refused. Not covered, and not worth asking for again."""
    return windows(base, source, table, symbol, scope, statuses=(DENIED,))


def listed_at(base: Path, source: str, symbol: str) -> dt.date | None:
    """When this source's expiration listing for `symbol` was last fetched.

    The listing is what an option plan divides work by, so its age bounds what
    "covered" can honestly mean: a listing fetched before a window ended cannot
    know about expirations listed since. `None` means it was never fetched.
    """
    settled = windows(base, source, "expirations", symbol)
    return max((w.end for w in settled), default=None)


def rows_for(base: Path, source: str, table: str, symbol: str) -> int:
    """Rows this ledger says were ingested. A cross-check on the store, not a source of truth."""
    total = _query(
        base,
        "SELECT COALESCE(SUM(rows), 0) FROM read_parquet(?) "
        'WHERE source = ? AND "table" = ? AND symbol = ?',
        [source, table, symbol],
    )
    return int(total[0][0]) if total else 0


def symbols(base: Path, source: str, table: str) -> list[str]:
    """Every symbol the ledger has an entry for."""
    rows = _query(
        base,
        'SELECT DISTINCT symbol FROM read_parquet(?) WHERE source = ? AND "table" = ? '
        "ORDER BY symbol",
        [source, table],
    )
    return [r[0] for r in rows]
