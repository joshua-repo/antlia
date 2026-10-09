"""The live read surface. Nothing here writes, and nothing here caches.

`live.fx` keeps its six-hour rate cache as the one documented exception; a
quote is never cached, because a cached quote is a stale quote wearing a
fresh timestamp.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from antlia.live import registry
from antlia.live.types import Snapshot
from antlia.schema import OPTION_QUOTE, Dataset

#: Datasets a live read can return, by name.
DATASETS: dict[str, Dataset] = {OPTION_QUOTE.name: OPTION_QUOTE}


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def canonical(raw: Any, dataset: str, projection: dict[str, str]) -> Any:
    """`raw` projected onto the dataset's columns, ordered by its natural key.

    The same `{column: SQL}` map `history` applies to a recorded frame, run by
    the same engine, so a snapshot and its recording cannot drift apart.
    """
    from antlia.live.engine import duckdb

    spec = DATASETS[dataset]
    missing = [c.name for c in spec.columns if c.name not in projection]
    if missing:
        raise KeyError(f"projection for {dataset} lacks: {', '.join(missing)}")
    select = ", ".join(f"{projection[c.name]} AS {_quote(c.name)}" for c in spec.columns)
    order = ", ".join(_quote(k) for k in spec.key)
    con = duckdb.connect()
    try:
        # Pinned as `history.store.connect` pins it. Without this a snapshot's
        # timestamps come back in the machine's zone and its recording in UTC:
        # the same instants, and two tables that no longer compare equal.
        con.execute("SET TimeZone='UTC'")
        con.register("vendor", raw)
        result = con.execute(f"SELECT {select} FROM vendor ORDER BY {order}")
        getter = getattr(result, "to_arrow_table", None) or result.fetch_arrow_table
        return getter()
    finally:
        con.close()


def option_chain(
    symbol: str,
    *,
    source: str = "ibkr",
    profile: str | None = None,
    expirations: Sequence[str | dt.date] | None = None,
    max_dte: int | None = None,
    min_dte: int | None = None,
    strikes: tuple[float, float] | None = None,
    rights: str = "CP",
) -> Snapshot:
    """A slice of `symbol`'s option chain, quoted now.

    Narrow it: a live chain is one request per contract against a broker that
    caps simultaneous market-data lines. `expirations`, or `min_dte`/`max_dte`,
    choose the expirations; `strikes=(lo, hi)` the strike band (by default the
    source picks a band around the underlying); `rights` is `"C"`, `"P"` or
    `"CP"`.

    The result is a `Snapshot`: `.table` in `antlia.schema.OPTION_QUOTE`'s
    columns, `.raw` as the broker sent it. Pass it to `history.record()` to
    keep it; reading it does not.
    """
    adapter = registry.get(source)
    if OPTION_QUOTE.name not in adapter.datasets:
        raise ValueError(f"live source {source!r} does not serve {OPTION_QUOTE.name}")
    raw = adapter.fetch(
        OPTION_QUOTE.name,
        symbol,
        profile=profile,
        expirations=expirations,
        max_dte=max_dte,
        min_dte=min_dte,
        strikes=strikes,
        rights=rights,
    )
    received = dt.datetime.now(dt.UTC)
    if raw is None:
        from antlia.live.engine import pa

        empty = pa.table({c.name: pa.array([], type=pa.null()) for c in OPTION_QUOTE.columns})
        return Snapshot(OPTION_QUOTE.name, adapter.name, received, empty, None)
    table = canonical(raw, OPTION_QUOTE.name, adapter.projection(OPTION_QUOTE.name))
    return Snapshot(OPTION_QUOTE.name, adapter.name, received, table, raw)


__all__ = ["DATASETS", "canonical", "option_chain"]
