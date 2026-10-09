"""Cached historical market data, in one shape, whichever vendor answered.

    from antlia import history

    history.equity_eod("AAPL", "2026-01-02", "2026-08-28")
    history.option_eod("AAPL", "2026-08-03", "2026-08-28", max_dte=45)
    history.coverage("option_eod", "AAPL", "2026-01-01", "2026-08-28")

**Cache-first.** A window the store already holds costs zero vendor requests
and never authenticates -- which is the property that makes a backtest
repeatable, and on a metered plan the property that makes it affordable at all.
What is missing is fetched, appended to `raw/`, and recorded, so the second run
of the same backtest is free.

**`fetch=` decides what happens when the store falls short**, and it is a
parameter rather than a policy because both answers are right at different
times:

    history.option_eod("AAPL", start, end)                # fill the gaps
    history.option_eod("AAPL", start, end, fetch=False)   # NotCovered, naming the gaps

Default `True`. `$ANTLIA_HISTORY_FETCH=0` flips the default process-wide, which
is how a backtest run is made provably offline without editing its call sites.

**One schema, one source per read.** `history.types` defines the columns; the
vendor's own shape stops in `raw/` and is mapped by SQL at read time. A read
never stitches two vendors together -- the same rule `fx` holds for rate
tables, and for the same reason: a total assembled from two vendors reconciles
against neither.

**Arrow by default.** `frame="pandas"` or `frame="polars"` converts, importing
that library only when asked. Nothing here forces a dataframe choice on a
consumer, and `pyarrow` is already the store's own machinery.

Adding a vendor is a `HistorySource` -- a `fetch` that calls their client and a
`projection` that names their columns. See `history.base`; the reader, the
cache, the coverage ledger and every consumer stay as they are.

Requires the `store` extra: `pip install antlia[store,thetadata]`.
"""

from __future__ import annotations

from antlia.history import types
from antlia.history.base import HistorySource, Scope
from antlia.history.errors import HistoryError, IngestFailed, NotCovered, NotEntitled
from antlia.history.ingest import Plan, Report
from antlia.history.listing import TTL as EXPIRATIONS_TTL
from antlia.history.listing import expirations
from antlia.history.reads import (
    ENV_FETCH,
    coverage,
    equity_eod,
    ingests,
    option_eod,
    option_quotes,
    rate_daily,
    symbols,
)
from antlia.history.registry import chain, register, source, unregister

# `history.path()` is the consumer's name for the store root; `store.root` is
# the implementation's. Renamed here rather than wrapped, because a wrapper
# would be logic and this file holds none.
from antlia.history.store import root as path
from antlia.history.types import Coverage, TableSpec, Window, window
from antlia.history.writes import check, fill, plan, record
from antlia.schema import CALL, PUT

__all__ = [
    "CALL",
    "ENV_FETCH",
    "EXPIRATIONS_TTL",
    "PUT",
    "Coverage",
    "HistoryError",
    "HistorySource",
    "IngestFailed",
    "NotCovered",
    "NotEntitled",
    "Plan",
    "Report",
    "Scope",
    "TableSpec",
    "Window",
    "chain",
    "check",
    "coverage",
    "equity_eod",
    "expirations",
    "fill",
    "ingests",
    "option_eod",
    "option_quotes",
    "path",
    "plan",
    "rate_daily",
    "record",
    "register",
    "source",
    "symbols",
    "types",
    "unregister",
    "window",
]
