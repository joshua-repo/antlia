"""The Parquet store, and DuckDB reading it directly.

Layout, which is the whole design:

    <root>/raw/<source>/<table>/session_date=YYYY-MM-DD/<stamp>-<pid>-<n>.parquet
    <root>/raw/<source>/expirations/<stamp>-<pid>.parquet
    <root>/ledger/<stamp>-<pid>.parquet

**`raw/` is immutable and append-only, and holds the vendor's own columns.**
Nothing is renamed, converted, dropped or repaired on the way in. Two columns
are added -- `source` and `ingested_at` -- and those are provenance, not
cleaning: they are what makes a vendor restatement a *new append* rather than
an edit, which is the property that lets a backtest ask what a source said
about date D as of time T.

**One derived value exists at write time and only one: `session_date`.** It is
the partition key, computed from the source's own `date` projection, and it
lives in the *path* -- pyarrow keeps partition columns out of the files, so a
raw file really is the vendor's frame plus two provenance columns. Partitioning
by date is the decision recorded in CLAUDE.md: date is the one key every table
shares, and one scheme beats a per-table judgement call. The cost is that
reconstructing one expiration's life touches every date partition; the fix for
that, when a backtest is actually slow, is a derived table partitioned by
expiration -- not a view, which would still scan.

**Normalisation happens here, at read time, in SQL.** The source's
`projection()` is a `{canonical column: expression}` map, and the reader
applies it over the raw scan. That is what makes the read API one schema while
`raw/` stays vendor-native, and it is why a vendor's mistake can be re-mapped
later instead of having been baked into the files.

**A read never mixes sources.** One `raw/<source>/` tree answers, or another
does. Two vendors' rows in one frame reconcile against neither of them, the
same rule `fx` holds for rate tables.

`derived/` is deliberately absent. It would have nothing in it today, and the
layout leaves room for it beside `raw/` so adding one later moves no files.
"""

from __future__ import annotations

import datetime as dt
import itertools
import os
import shutil
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from antlia.auth.credentials import home
from antlia.history.engine import duckdb, pa
from antlia.history.types import DATED, PROVENANCE, TableSpec

#: Where the store lives when nothing says otherwise. Beside the credentials
#: file, so one directory holds everything antlia keeps on a machine.
ENV_ROOT = "ANTLIA_STORE"

#: The store-owned partition column. Named for what it is, and named so it
#: cannot collide with a vendor column called `date`.
PARTITION = "session_date"

#: Where a write is assembled before it is moved into `raw/`. Under the store
#: root so the move is a rename on one filesystem, and outside `raw/` so a
#: half-written dataset is never in the read glob.
STAGING = ".staging"

#: Where `check --repair` puts a file it cannot read.
QUARANTINE = ".quarantine"

_lock = threading.Lock()

#: Makes a staging directory unique within one process, so parallel writes
#: sharing a timestamp and a pid still cannot collide.
_serial = itertools.count()


def root(override: str | Path | None = None) -> Path:
    """The store root: an argument, then `$ANTLIA_STORE`, then `~/.antlia/store`.

    Exported as `history.path`, which is the name a consumer uses.
    """
    if override is not None:
        return Path(override).expanduser()
    env = os.environ.get(ENV_ROOT)
    if env:
        return Path(env).expanduser()
    return home() / "store"


def table_dir(base: Path, source: str, table: str) -> Path:
    return base / "raw" / source / table


def ledger_dir(base: Path) -> Path:
    return base / "ledger"


def staging_dir(base: Path) -> Path:
    return base / STAGING


def quarantine_dir(base: Path) -> Path:
    return base / QUARANTINE


def connect() -> duckdb.DuckDBPyConnection:
    """An in-process DuckDB over the Parquet files. No server, no load step."""
    con = duckdb.connect()
    # Every timestamp the store reasons about is either tz-aware or a session
    # date. Pinning the session zone stops a machine's locale changing what a
    # cast means.
    con.execute("SET TimeZone='UTC'")
    return con


def _glob(base: Path, source: str, table: str) -> str | None:
    """The read_parquet pattern for a table, or None when nothing is stored."""
    directory = table_dir(base, source, table)
    if not directory.exists():
        return None
    pattern = "**/*.parquet"
    if not any(directory.glob(pattern)):
        return None
    return str(directory / pattern)


def _table(result: Any) -> pa.Table:
    """The arrow table out of a DuckDB result, across the rename in 1.4.

    `fetch_arrow_table()` is what 1.0-1.3 offer and 1.4 deprecates in favour of
    `to_arrow_table()`. `arrow()` is not the fallback it looks like: in 1.4 it
    returns a `RecordBatchReader`, so a version bump would silently change the
    type this module hands out.
    """
    getter = getattr(result, "to_arrow_table", None) or result.fetch_arrow_table
    return getter()


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def empty(spec: TableSpec, con: duckdb.DuckDBPyConnection | None = None) -> pa.Table:
    """A zero-row table with the canonical schema.

    An empty store answers with the right *shape*, not with `None`. A consumer
    that has to branch on "no file yet" has been handed a storage detail.
    """
    owned = con is None
    con = con or connect()
    try:
        casts = ", ".join(f"CAST(NULL AS {c.type}) AS {_quote(c.name)}" for c in spec.all_columns)
        return _table(con.execute(f"SELECT {casts} WHERE false"))
    finally:
        if owned:
            con.close()


def write(
    base: Path,
    source: str,
    spec: TableSpec,
    frame: pa.Table,
    date_expression: str | None,
    *,
    ingested_at: dt.datetime | None = None,
) -> int:
    """Append one vendor frame to `raw/`. Returns the row count written.

    `date_expression` is the source's own `date` projection, evaluated here to
    produce the partition value. It is required for dated tables and ignored
    for the rest.
    """
    if frame.num_rows == 0:
        return 0
    stamp = ingested_at or dt.datetime.now(dt.UTC)
    con = connect()
    try:
        con.register("vendor", frame)
        columns = ", ".join(_quote(n) for n in frame.column_names)
        extra = f", {date_expression} AS {PARTITION}" if spec.name in DATED else ""
        if spec.name in DATED and not date_expression:
            raise ValueError(f"{spec.name} is partitioned by date; the source gave no expression")
        prepared = _table(
            con.execute(
                f"SELECT {columns}, ? AS source, ?::TIMESTAMPTZ AS ingested_at{extra} FROM vendor",
                [source, stamp],
            )
        )
    finally:
        con.close()

    # Deferred: `pyarrow.dataset` imports pandas when it is installed, and a
    # read has no use for either. Keeping it inside the writer is what lets
    # `import antlia.history` stay clear of a consumer's dataframe library.
    import pyarrow.dataset as ds

    directory = table_dir(base, source, spec.name)
    tag = f"{stamp:%Y%m%dT%H%M%S%f}-{os.getpid()}-{next(_serial)}"
    partitioning = None
    if spec.name in DATED:
        partitioning = ds.partitioning(
            pa.schema([(PARTITION, prepared.schema.field(PARTITION).type)]), flavor="hive"
        )

    # **Staged, then renamed into place.** Writing straight to the final name
    # means a killed process leaves a truncated Parquet file -- and because the
    # reader unions the whole glob to align schemas, one truncated file makes
    # *every* date in the table unreadable, not just its own. That poisons a
    # store rather than losing a file. A rename within one filesystem is
    # atomic, so a reader sees a file either whole or not at all. `fx.cache`
    # has always done this; this tier did not, and it was the more expensive
    # place to get it wrong.
    staging = staging_dir(base) / tag
    try:
        staging.mkdir(parents=True, exist_ok=True)
        ds.write_dataset(
            prepared,
            staging,
            format="parquet",
            partitioning=partitioning,
            basename_template=f"{tag}-{{i}}.parquet",
            existing_data_behavior="overwrite_or_ignore",
        )
        staged = sorted(staging.rglob("*.parquet"))
        # Only the renames are serialised: encoding Parquet is the slow part
        # and holding the lock across it would serialise a parallel ingest for
        # no reason.
        with _lock:
            for path in staged:
                target = directory / path.relative_to(staging)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return int(prepared.num_rows)


def read(
    base: Path,
    source: str,
    spec: TableSpec,
    projection: dict[str, str],
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
    where: Sequence[str] = (),
    params: Sequence[Any] = (),
    latest: bool = True,
    as_of: dt.datetime | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> pa.Table:
    """The canonical view of one source's raw files.

    `latest=True` keeps the newest append per natural key, which is what makes
    a vendor restatement resolve **at read time** rather than at write time.
    `latest=False` returns every version, which is the point of keeping them.

    `as_of` hides every append that landed after it, so the two combine into
    the question this whole tier exists to answer: *what did this source say
    about date D, as of time T*. With `latest=True` that is the newest version
    a reader would have seen at T; with `latest=False` it is every version up
    to T. Applied before the ranking, so an append made yesterday cannot
    displace the one a study actually ran on.

    `start`/`end` filter the partition column before the scan, so a narrow
    window over a wide store reads only the directories it needs.
    """
    owned = con is None
    con = con or connect()
    try:
        pattern = _glob(base, source, spec.name)
        if pattern is None:
            return empty(spec, con)

        missing = [c.name for c in spec.columns if c.name not in projection]
        if missing:
            raise KeyError(f"{source} projection for {spec.name} lacks: {', '.join(missing)}")

        scan_where: list[str] = []
        scan_params: list[Any] = []
        if spec.name in DATED:
            if start is not None:
                scan_where.append(f"{PARTITION} >= ?")
                scan_params.append(start)
            if end is not None:
                scan_where.append(f"{PARTITION} <= ?")
                scan_params.append(end)
        if as_of is not None:
            scan_where.append("ingested_at <= ?")
            scan_params.append(as_of)
        scan_filter = (" WHERE " + " AND ".join(scan_where)) if scan_where else ""

        # The partition column already holds what the `date` projection
        # computes, so reading it back is both cheaper and provably identical.
        exprs = dict(projection)
        if spec.name in DATED:
            exprs["date"] = PARTITION
        select = ", ".join(f"{exprs[c.name]} AS {_quote(c.name)}" for c in spec.columns)
        select += ", " + ", ".join(_quote(c.name) for c in PROVENANCE)

        key = ", ".join(_quote(k) for k in spec.key)
        rank = f"row_number() OVER (PARTITION BY {key} ORDER BY ingested_at DESC) AS __rn"
        inner = (
            f"SELECT {select}, {rank} FROM read_parquet(?, hive_partitioning=true, "
            f"union_by_name=true){scan_filter}"
        )

        outer_where = list(where)
        if latest:
            outer_where.append("__rn = 1")
        clause = (" WHERE " + " AND ".join(outer_where)) if outer_where else ""
        order = key if latest else f"{key}, ingested_at"
        columns = ", ".join(_quote(n) for n in spec.names)
        sql = f"SELECT {columns} FROM ({inner}) AS t{clause} ORDER BY {order}"
        return _table(con.execute(sql, [pattern, *scan_params, *params]))
    finally:
        if owned:
            con.close()


def ingests(
    base: Path,
    source: str,
    spec: TableSpec,
    symbol: str | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> list[dt.datetime]:
    """Every distinct `ingested_at` held, oldest first.

    These are the moments `as_of` can take. Without them a caller has a
    parameter that wants a timestamp and no way to learn which timestamps
    exist, so the restatement machinery is present but unusable unless you kept
    the value from an earlier read.

    **One entry per write, not per run.** A fill of forty expirations leaves
    forty stamps, and that is deliberate: `as_of` half way through a run then
    shows exactly the rows that existed half way through it. Stamping a whole
    run with its start time would read tidier and would show you rows from the
    future.

    Read from the rows rather than from the ledger on purpose: the ledger's own
    stamp is when the bookkeeping was flushed, seconds after the rows were
    written, and `as_of` filters on the rows.
    """
    owned = con is None
    con = con or connect()
    try:
        pattern = _glob(base, source, spec.name)
        if pattern is None:
            return []
        where = ""
        params: list[Any] = [pattern]
        if symbol is not None:
            where = " WHERE symbol = ?"
            params.append(symbol)
        rows = con.execute(
            "SELECT DISTINCT ingested_at FROM read_parquet(?, hive_partitioning=true, "
            f"union_by_name=true){where} ORDER BY ingested_at",
            params,
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        if owned:
            con.close()


def sources(base: Path) -> list[str]:
    """Source names with something in the store, whichever adapters are registered."""
    raw = base / "raw"
    if not raw.exists():
        return []
    return sorted(p.name for p in raw.iterdir() if p.is_dir())


def size(base: Path, source: str, table: str) -> tuple[int, int]:
    """`(files, bytes)` held for one table. For the doctor, not for logic."""
    directory = table_dir(base, source, table)
    if not directory.exists():
        return (0, 0)
    files = list(directory.glob("**/*.parquet"))
    return (len(files), sum(f.stat().st_size for f in files))
