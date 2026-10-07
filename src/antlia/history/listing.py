"""The expiration listing, and what an option plan is allowed to assume from it.

Small, and its own module because it is the joint everything else turns on --
and the source of the two worst defects this layer has had:

- **A fully covered read authenticated**, because the planner asked the vendor
  which expirations exist before it could work out that it needed nothing.
- **`coverage()` reported `complete`** against a listing from months earlier,
  while whole expirations had never been fetched.

Both come from the same fact: an option plan divides work by this listing, so
the listing decides both what gets fetched and what "covered" can honestly
mean. Keeping it in one file is what stops each caller growing its own slightly
different answer -- which is exactly how the first bug got in, with three
callers carrying three different fetch policies.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from antlia.history import ingest, ledger, registry

# `store` is the keyword every public function takes for the store root, and
# inside those functions the parameter shadows the module. Aliasing is what
# keeps this package out of that name collision.
from antlia.history import store as storage
from antlia.history.errors import NotCovered
from antlia.history.types import Window, as_date, table

#: Expirations are a listing about a symbol as of today, not an observation
#: about a session, so they are refreshed once a day rather than by window.
TTL = dt.timedelta(days=1)


def expirations(
    symbol: str,
    *,
    source: str | None = None,
    fetch: bool | None = None,
    store: str | Path | None = None,
) -> list[dt.date]:
    """Every expiration a source lists for `symbol`, oldest first.

    Cached in the store so the option planner can work out what to fetch
    **without authenticating** -- otherwise every "already covered" read would
    still open a session just to learn which expirations exist, and the
    cache-first guarantee would be a fiction.
    """
    from antlia.history.reads import default_fetch

    base, src = registry.bind(source, store)
    spec = table("expirations")
    today = dt.date.today()

    if fetch is None:
        fetch = default_fetch()
    if fetch:
        settled = ledger.windows(base, src.name, spec.name, symbol)
        if not any(today - w.end < TTL for w in settled):
            ingest.run(base, src, spec, symbol, Window(today, today))

    data = storage.read(base, src.name, spec, src.projection(spec.name))
    listed = data.column("expiration").to_pylist()
    owners = data.column("symbol").to_pylist()
    return sorted({as_date(e) for e, owner in zip(listed, owners, strict=True) if owner == symbol})


def planned(
    table_name: str,
    symbol: str,
    options: dict[str, Any],
    *,
    source: str | None,
    store: str | Path | None,
    fetch: bool | None,
    required: bool = True,
) -> dict[str, Any] | None:
    """`options` with an option plan's expiration listing filled in from the store.

    There is exactly one copy of this because the callers want three different
    fetch policies and got them subtly wrong when each carried its own: a read
    fetches if allowed, a fill always warms the listing, and `coverage()` -- a
    question purely about the store -- must never call the vendor at all.

    `required=False` returns None instead of raising, for the caller that has
    its own answer to "the listing is not here": `coverage()` reports the whole
    window missing, which is true, rather than complete, which is not.
    """
    if table_name != "option_eod" or "expirations" in options:
        return options
    listed = expirations(symbol, source=source, fetch=fetch, store=store)
    if listed:
        return {**options, "expirations": listed}
    if not required:
        return None
    # Names the source without importing its adapter: an error about a missing
    # cache must not be the thing that demands a vendor SDK. The fix is spelled
    # out because "the expiration listing is not cached" is true but not
    # actionable -- filling `option_eod` warms the listing as a side effect.
    raise NotCovered(
        "expirations",
        symbol,
        [],
        source or registry.default(),
        fix=f"antlia-history fill option_eod {symbol} --start ... --end ...",
    )
