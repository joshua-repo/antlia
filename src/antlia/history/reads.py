"""The read surfaces: canonical rows out of the store, in one shape.

Every function here is a *read*. Nothing in this file writes to `raw/` -- that
is `history.writes`, and keeping the two apart in the layout is what makes the
layer's central rule visible: **several read surfaces, exactly one write path**.

`fetch=` is the one place a read may cause a write, and it does so by calling
the write path rather than by writing itself.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from antlia.history import ingest, ledger, listing, registry
from antlia.history import store as storage
from antlia.history.errors import NotCovered
from antlia.history.types import LISTED, Coverage, Frame, Window, as_date, merge
from antlia.history.types import table as spec_for
from antlia.history.types import window as window_for
from antlia.schema import CALL, PUT

#: Flips the `fetch=` default for the whole process. Set it to 0/false and a
#: read that is not covered raises instead of quietly spending requests.
ENV_FETCH = "ANTLIA_HISTORY_FETCH"


def default_fetch() -> bool:
    """Whether a read fills its own gaps when nothing says otherwise."""
    raw = os.environ.get(ENV_FETCH)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _match(symbol: str | Sequence[str]) -> tuple[str, list[str]]:
    """A `symbol` predicate for one name or several, and the names it binds.

    Reads take a universe because a pooled backtest wants one frame with a
    `symbol` column, not N frames it has to concatenate. The write path stays
    single-symbol: a `Report` is about one symbol, and a function whose return
    type changes with the shape of an argument is worse than a loop.
    """
    names = [symbol] if isinstance(symbol, str) else list(dict.fromkeys(symbol))
    if not names:
        raise ValueError("no symbol given")
    if len(names) == 1:
        return '"symbol" = ?', names
    return f'"symbol" IN ({", ".join("?" for _ in names)})', names


def _as_frame(data: Any, frame: Frame) -> Any:
    if frame == "arrow":
        return data
    if frame == "pandas":
        return data.to_pandas()
    if frame == "polars":
        import polars

        return polars.from_arrow(data)
    raise ValueError(f"unknown frame {frame!r}; use 'arrow', 'pandas' or 'polars'")


def _right(value: str) -> str:
    text = str(value).strip().upper()
    if text in {"C", "CALL"}:
        return CALL
    if text in {"P", "PUT"}:
        return PUT
    raise ValueError(f"unknown right {value!r}; use 'C'/'call' or 'P'/'put'")


def _read(
    table_name: str,
    symbol: str | Sequence[str],
    start: str | dt.date,
    end: str | dt.date,
    *,
    source: str | None,
    fetch: bool | None,
    frame: Frame,
    store: str | Path | None,
    latest: bool,
    as_of: dt.datetime | None,
    workers: int,
    where: list[str],
    params: list[Any],
    **options: Any,
) -> Any:
    base, src = registry.bind(source, store)
    spec = spec_for(table_name)
    span = window_for(start, end)
    predicate, names = _match(symbol)

    if fetch is None:
        fetch = default_fetch()

    for name in names:
        # Per symbol, because the expiration listing is per symbol: planning a
        # universe against one member's chain would fetch the wrong contracts
        # for every other member of it.
        scoped = listing.planned(spec.name, name, options, source=source, store=store, fetch=fetch)
        scoped = scoped or {}
        if fetch:
            ingest.run(base, src, spec, name, span, workers=workers, **scoped)
            continue
        intent = ingest.plan(base, src, spec, name, span, **scoped)
        if intent.requests:
            # The first uncovered symbol names itself and the command that
            # fixes it. Naming all of them would produce a hint you cannot run.
            raise NotCovered(spec.name, name, merge(r.window for r in intent.requests), src.name)

    data = storage.read(
        base,
        src.name,
        spec,
        src.projection(spec.name),
        start=span.start,
        end=span.end,
        where=[predicate, *where],
        params=[*names, *params],
        latest=latest,
        as_of=as_of,
    )
    return _as_frame(data, frame)


def equity_eod(
    symbol: str | Sequence[str],
    start: str | dt.date,
    end: str | dt.date,
    *,
    source: str | None = None,
    fetch: bool | None = None,
    frame: Frame = "arrow",
    store: str | Path | None = None,
    latest: bool = True,
    as_of: dt.datetime | None = None,
    workers: int = 1,
) -> Any:
    """Daily bars: OHLCV plus the closing NBBO, one row per symbol per session.

    `symbol` may be a list, in which case one frame comes back and the `symbol`
    column tells the rows apart.

    `latest=False` returns every append rather than the newest per session, and
    `as_of` hides everything ingested after a moment -- together they are how
    you ask what a source said about a date *as of* a past ingest, which is the
    reason restatements resolve at read time and not at write.
    """
    return _read(
        "equity_eod",
        symbol,
        start,
        end,
        source=source,
        fetch=fetch,
        frame=frame,
        store=store,
        latest=latest,
        as_of=as_of,
        workers=workers,
        where=[],
        params=[],
    )


def option_eod(
    symbol: str | Sequence[str],
    start: str | dt.date,
    end: str | dt.date,
    *,
    expiration: str | dt.date | None = None,
    right: str | None = None,
    strikes: tuple[float, float] | None = None,
    dte: tuple[int, int] | None = None,
    max_dte: int | None = None,
    min_dte: int | None = None,
    source: str | None = None,
    fetch: bool | None = None,
    frame: Frame = "arrow",
    store: str | Path | None = None,
    latest: bool = True,
    as_of: dt.datetime | None = None,
    workers: int = 1,
) -> Any:
    """Daily bars for a symbol's option chain: one row per contract per session.

    The filters split into two kinds, and the difference costs real money:

    - **`max_dte` / `min_dte` shape what gets fetched.** They drop whole
      expirations from the plan, so a warm-up asking for 45-day options does
      not pay for five-year LEAPs.
    - **`expiration`, `right`, `strikes`, `dte` shape what comes back.** They
      filter rows already in the store and cost nothing.

    `dte` is `(low, high)` inclusive, in calendar days from the session to the
    expiration -- date to date, so a contract expiring on the session itself is
    0, not -1.
    """
    where: list[str] = []
    params: list[Any] = []
    if expiration is not None:
        where.append('"expiration" = ?')
        params.append(as_date(expiration))
    if right is not None:
        where.append('"right" = ?')
        params.append(_right(right))
    if strikes is not None:
        where.append('"strike" BETWEEN ? AND ?')
        params.extend([float(strikes[0]), float(strikes[1])])
    if dte is not None:
        where.append('date_diff(\'day\', "date", "expiration") BETWEEN ? AND ?')
        params.extend([int(dte[0]), int(dte[1])])

    shape: dict[str, Any] = {}
    if max_dte is not None:
        shape["max_dte"] = max_dte
    if min_dte is not None:
        shape["min_dte"] = min_dte
    if dte is not None:
        # A row filter implies the fetch plan: there is no point paying for
        # expirations whose every row this read would then discard.
        shape.setdefault("min_dte", dte[0])
        shape.setdefault("max_dte", dte[1])
    if expiration is not None:
        shape["expirations"] = [as_date(expiration)]

    return _read(
        "option_eod",
        symbol,
        start,
        end,
        source=source,
        fetch=fetch,
        frame=frame,
        store=store,
        latest=latest,
        as_of=as_of,
        workers=workers,
        where=where,
        params=params,
        **shape,
    )


def coverage(
    table_name: str,
    symbol: str,
    start: str | dt.date | None = None,
    end: str | dt.date | None = None,
    *,
    source: str | None = None,
    store: str | Path | None = None,
    **options: Any,
) -> Coverage:
    """What the store holds for a table and symbol, and what it does not.

    Answers **completeness, never quality**: whether every day in the window is
    accounted for, not whether the prices are any good. The second question is
    a judgement call and belongs to the consumer, or to a derived table that
    says in its name which rule it applied.

    `missing` was never fetched. `denied` the source refused and will refuse
    again. Keeping them apart is what stops a fetch loop retrying a permanent
    answer forever.

    For an option table the answer is only as good as the expiration listing it
    was computed from, so `Coverage.listed_at` carries when that was fetched
    and `complete` is False while it is too old to vouch for the window. A gap
    in an expiration the listing never knew about is not a gap this function
    could have found.
    """
    base, src = registry.bind(source, store)
    spec = spec_for(table_name)

    held = ledger.windows(base, src.name, spec.name, symbol)
    refused = ledger.denied(base, src.name, spec.name, symbol)
    rows = ledger.rows_for(base, src.name, spec.name, symbol)
    listed = ledger.listed_at(base, src.name, symbol) if spec.name in LISTED else None

    def report(requested: Window | None, missing: list[Window], denied: list[Window]) -> Coverage:
        """The three fields that vary, over the six that do not."""
        return Coverage(
            table=spec.name,
            symbol=symbol,
            source=src.name,
            requested=requested,
            held=held,
            missing=missing,
            denied=denied,
            rows=rows,
            listed_at=listed,
        )

    if start is None or end is None:
        return report(None, [], refused)

    requested = window_for(start, end)
    # A question about the store is answered from the store: never a vendor
    # call, and "no listing" means the window is missing, not complete.
    scoped = listing.planned(
        spec.name, symbol, options, source=source, store=store, fetch=False, required=False
    )
    if scoped is None:
        return report(requested, [requested], refused)

    intent = ingest.plan(base, src, spec, symbol, requested, **scoped)
    if intent.beyond_horizon is not None:
        # Outside the plan's entitlement. It is not missing -- nothing will
        # ever fill it -- and reporting it as complete would let a backtest run
        # over a window a third of which the source cannot serve.
        refused = merge([*refused, intent.beyond_horizon])
    missing = [r.window for r in intent.requests]
    if intent.unpublished is not None:
        # Not requested yet because the source has not summarised it, but
        # still not held: before the close, a window ending today is missing
        # today, and calling it complete is the wrong answer the bound exists
        # to prevent.
        missing.append(intent.unpublished)
    return report(requested, merge(missing), refused)


def ingests(
    table_name: str,
    symbol: str | None = None,
    *,
    source: str | None = None,
    store: str | Path | None = None,
) -> list[dt.datetime]:
    """The moments `as_of` can be given, oldest first.

    Each is when one write landed -- one per vendor request, not one per fill,
    so a forty-expiration warm-up leaves forty. That is what makes `as_of` mean
    "what existed at this instant" rather than "what the run eventually
    produced". To reproduce a past study use the timestamp that study recorded;
    to reproduce the state before a restatement, the last one from before it.
    """
    base, src = registry.bind(source, store)
    return storage.ingests(base, src.name, spec_for(table_name), symbol)


def symbols(
    table_name: str, *, source: str | None = None, store: str | Path | None = None
) -> list[str]:
    """Every symbol the store has been asked about for this table."""
    base, src = registry.bind(source, store)
    return ledger.symbols(base, src.name, spec_for(table_name).name)


__all__ = [
    "ENV_FETCH",
    "coverage",
    "default_fetch",
    "equity_eod",
    "ingests",
    "option_eod",
    "symbols",
]
