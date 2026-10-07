"""The history-source contract -- the seam a second vendor slots into.

Everything above this file is vendor-agnostic: the store layout, the coverage
ledger, the incremental fetch plan, the canonical read. A source supplies four
things and nothing else:

1. **What it can serve** -- `tables`; `earliest()`, the oldest date the
   plan is entitled to; and `latest()`, the newest session it has published.
   A metered key has a horizon; asking past it is not a retryable failure,
   and the planner needs to know where it is *before* it spends a request
   finding out.
2. **How work divides** -- `scopes()`. An equity table is one unit of work per
   symbol; an option table is one per expiration, because that is the shape of
   every options API. A scope may carry its own bound: an expiration that
   expired in March has nothing to say about April, and clamping there removes
   the single largest source of wasted requests when warming a cache.
3. **How to fetch one unit** -- `fetch()`, returning **the vendor's own frame,
   unmodified**. Renaming a column here would be normalisation on the way into
   `raw/`, which is precisely what the storage rule forbids.
4. **How its frame maps onto the canonical schema** -- `projection()`, a
   `{canonical column: SQL expression}` map evaluated by DuckDB **at read
   time**, over the raw files. This is where `'CALL'` becomes `'C'` and a
   vendor's thousandths become dollars.

The projection is the whole reason this seam works. Adding Databento or
Massive is a `fetch` that calls their client and a projection that names their
columns -- no reader, no cache, no ingest logic changes, and no consumer of
`history` can tell which vendor answered.

A source authenticates **through `antlia.auth`**, exactly as `fx`
does, and never reimplements resolution, pooling or rate limiting.
"""

from __future__ import annotations

import abc
import datetime as dt
import importlib
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from antlia.auth.errors import MissingExtra
from antlia.history.engine import pa
from antlia.history.types import Window


@dataclass(frozen=True, slots=True)
class Scope:
    """One independently-fetchable slice of a table, for one symbol.

    `key` names it in the coverage ledger and is empty for tables that have no
    sub-division. `bound` is the widest window the slice can possibly hold --
    an expiration's life, say -- and lets the planner drop work before it is
    dispatched rather than discovering emptiness one request at a time.
    """

    key: str = ""
    bound: Window | None = None

    def clamp(self, window: Window) -> Window | None:
        if self.bound is None:
            return window
        return window.clamp(self.bound.start, self.bound.end)


class HistorySource(abc.ABC):
    """Historical market data from one vendor."""

    #: Name in the registry, in `raw/<source>/`, and in every row's `source`
    #: column. Changing it orphans a store, so treat it as permanent.
    name: str
    #: Source name in `antlia.auth`. None means the endpoint is keyless.
    auth_source: str | None = None
    #: pip extra and import name, for a readable failure when the SDK is absent.
    extra: str | None = None
    package: str | None = None
    #: Canonical tables this source can fill.
    tables: frozenset[str] = frozenset()
    #: Largest span, in calendar days, the vendor accepts in one request.
    #: None means unbounded. A request one day over the cap fails outright
    #: rather than being trimmed, so the planner must respect it exactly.
    max_span_days: int | None = None
    #: Most requests the vendor will serve at once. None means it has no stated
    #: limit. Like `max_span_days` this is the vendor's rule, not a preference:
    #: exceeding it fails the request rather than queueing it, so the ingest
    #: driver clamps to it and a caller may ask for more without knowing.
    max_workers: int | None = None

    def earliest(self, table: str) -> dt.date | None:
        """Oldest date this plan may ask for, or None for no known limit.

        Returned rather than stored because it is a property of the *plan*,
        not of the vendor: it moves when a subscription changes, and on a
        rolling window it moves every day.
        """
        return None

    def latest(self, table: str) -> dt.date | None:
        """Newest session this source has published, or None for no lag.

        The other end of `earliest()`, and just as much a fact the planner must
        know before it spends a request. A vendor asked about a session it has
        not summarised yet answers for the rest of the window and says nothing
        about that day -- and the ledger records the *requested* window as
        settled, so without this bound a fill run before the close marks
        today covered and never asks for it again.

        Per table, because a listing has no publication lag: what expires is
        known today, while today's closing quote is not.
        """
        return None

    @abc.abstractmethod
    def scopes(self, table: str, symbol: str, window: Window, **options: Any) -> list[Scope]:
        """The units of work covering `window`, in the order to fetch them."""

    @abc.abstractmethod
    def fetch(self, table: str, symbol: str, window: Window, scope: Scope) -> pa.Table | None:
        """One unit of work, as the vendor returned it.

        **Vendor-native.** No renaming, no unit conversion, no dropped columns,
        no repaired timestamps. `None` means the vendor answered and had no
        rows -- a market holiday, an expiration that never traded -- which is a
        fact worth recording, not a failure. `NotEntitled` means it refused and
        always will; anything else is transient.
        """

    @abc.abstractmethod
    def projection(self, table: str) -> dict[str, str]:
        """`{canonical column: SQL expression}` over this source's raw columns.

        Evaluated by DuckDB against the raw parquet at read time. Every column
        of the table's `TableSpec` must appear; `source` and `ingested_at` are
        supplied by the store and must not.
        """

    def verify(self) -> str:
        """One cheap real round-trip, for the doctor.

        The same three levels `auth` draws: that a source is registered is not
        evidence it answers, and that it authenticates is not evidence this
        plan may read what you are about to ask for.
        """
        raise NotImplementedError(f"{self.name} has no verify()")

    def require(self, module: str) -> ModuleType:
        try:
            return importlib.import_module(module)
        except ImportError as exc:
            raise MissingExtra(self.name, self.extra or self.name, self.package or module) from exc


def context(frame: pa.Table, **columns: Any) -> pa.Table:
    """The vendor's frame with the request's own parameters appended.

    Some vendor answers do not say what question they answer: ThetaData's stock
    EOD frame carries no `symbol`, because you told it the symbol. A raw file
    that cannot say what it is about is not raw data, it is an orphan -- so the
    adapter appends what it asked for.

    This is an **addition, never a modification**. The vendor's own columns are
    untouched, which is the property `raw/` actually depends on; a column that
    records the request is provenance, like `source` and `ingested_at`.
    """
    out = frame
    for name, value in columns.items():
        if name in out.column_names:
            continue
        out = out.append_column(name, pa.array([value] * out.num_rows))
    return out


def arrow(frame: Any) -> pa.Table | None:
    """Whatever the vendor's client returned, as an arrow table. Empty is None.

    Vendor SDKs hand back polars, pandas or arrow depending on the version and
    on a client setting -- ThetaData's is a constructor argument. All three
    speak the arrow C stream protocol, so `pa.table` converts every one of them
    without a per-library branch, and the conversion happens once here rather
    than in each adapter.

    Emptiness collapses to None because that is the distinction the ingest path
    acts on: a frame with no rows and no frame at all both mean "the vendor
    answered and had nothing", which is settled, not failed.
    """
    if frame is None:
        return None
    table = frame if isinstance(frame, pa.Table) else pa.table(frame)
    return table if table.num_rows else None
