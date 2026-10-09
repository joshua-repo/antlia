"""The write path, its dry run, and the store's own health check.

**Everything that reaches `raw/` comes through here**, by one of two doors:
`fill` for what a vendor is asked for, `record` for a live answer already in
hand. Both end in the same `store.write`, so there is still exactly one
writer -- the rule CLAUDE.md calls *several read surfaces, one write path*. A
read that wants to fill its own gaps calls in here rather than growing a
writer of its own.

`plan` is `fill` without the spending, and `check` is the store's account of
itself -- integrity, never quality.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from antlia.history import ingest, integrity, listing, registry
from antlia.history import store as storage
from antlia.history.ingest import Plan, Report
from antlia.history.types import RECORDED
from antlia.history.types import fetched as spec_for
from antlia.history.types import table as any_spec
from antlia.history.types import window as window_for


def fill(
    table_name: str,
    symbol: str,
    start: str | dt.date,
    end: str | dt.date,
    *,
    source: str | None = None,
    store: str | Path | None = None,
    limit: int | None = None,
    workers: int = 1,
    refresh: bool = False,
    progress: Any = None,
    **options: Any,
) -> Report:
    """Fetch and store whatever the window is missing. **The only write path.**

    Named `fill` rather than `ingest` because that is what it does: the module
    it delegates to is `history.ingest`, and a function and a module of one
    name in one namespace is a collision waiting to happen.

    `limit` caps the vendor requests, so a first look at a wide window can be
    taken without spending a day's budget. A capped run is not a failure -- the
    next call resumes from the ledger exactly where this one stopped.

    `workers` runs that many requests at once, clamped to the vendor's own
    ceiling and to 1 for an SDK `auth` declares single-threaded. It defaults to
    1 because raising it spends a metered budget faster, which is a decision
    for the caller.

    `refresh=True` re-fetches the window even where the ledger already covers
    it. That is the **only** way to record a vendor restatement: everything
    else in this layer subtracts what is already settled, so without it a key
    can never have a second version and `latest=`/`as_of=` have nothing to
    choose between.
    """
    spec = spec_for(table_name)
    base, src = registry.bind(source, store, table_name)
    # Warming the listing is part of the fill, so every later read of this
    # store -- and every `coverage()` -- can plan offline.
    scoped = listing.planned(table_name, symbol, options, source=source, store=store, fetch=True)
    return ingest.run(
        base,
        src,
        spec,
        symbol,
        window_for(start, end),
        limit=limit,
        workers=workers,
        refresh=refresh,
        progress=progress,
        **(scoped or {}),
    )


def plan(
    table_name: str,
    symbol: str,
    start: str | dt.date,
    end: str | dt.date,
    *,
    source: str | None = None,
    store: str | Path | None = None,
    refresh: bool = False,
    **options: Any,
) -> Plan:
    """What `fill` would ask the vendor for, without asking for any of it.

    An option plan does need the expiration listing to enumerate its scopes, so
    against a store that has never seen the symbol this costs the one listing
    call -- which is then cached, and never paid for again. None of the
    requests it describes are made.
    """
    spec = spec_for(table_name)
    base, src = registry.bind(source, store, table_name)
    scoped = listing.planned(table_name, symbol, options, source=source, store=store, fetch=None)
    return ingest.plan(
        base,
        src,
        spec,
        symbol,
        window_for(start, end),
        refresh=refresh,
        **(scoped or {}),
    )


def record(snapshot: Any, *, store: str | Path | None = None) -> int:
    """Keep a live answer. Returns the rows appended.

    `snapshot` is what a `live` read returned (an `antlia.live.Snapshot`). Its
    **raw** frame is what lands in `raw/`, vendor-native like every other
    append, and the snapshot's own source is the one whose projection reads it
    back -- so `history.option_quotes()` later returns exactly the rows
    `snapshot.table` held now.

    No ledger entry: a recording is a sample of one moment, and there is no
    complete set of moments for `coverage()` to measure against.
    """
    spec = any_spec(snapshot.dataset)
    if spec.name not in RECORDED:
        raise ValueError(f"{spec.name} is fetched, not recorded; use fill()")
    if snapshot.raw is None or snapshot.raw.num_rows == 0:
        return 0
    base, src = registry.bind(snapshot.source, store, spec.name)
    if spec.name not in src.tables:
        raise ValueError(f"history source {src.name!r} does not keep {spec.name}")
    date_expression = src.projection(spec.name).get("date")
    return storage.write(base, src.name, spec, snapshot.raw, date_expression)


def check(
    *, source: str | None = None, store: str | Path | None = None, repair: bool = False
) -> list[integrity.Problem]:
    """Everything wrong with the store itself, most damaging first.

    **Integrity, not quality**: unreadable files, rubbish left by a killed
    write, an expiration listing older than the data beside it, and the
    ledger's account disagreeing with what is on disk. Whether a quote is
    crossed is a judgement and stays the consumer's.

    `repair=True` quarantines unreadable files and clears leaked staging
    directories. A count mismatch is not repairable here -- only the vendor
    knows what should be there, so the fix is `fill(..., refresh=True)`.
    """
    base = storage.root(store)
    found = integrity.check(base, source)
    if repair:
        integrity.repair(base, found)
    return found


__all__ = ["check", "fill", "plan", "record"]
