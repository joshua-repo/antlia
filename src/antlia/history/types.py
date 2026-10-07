"""The canonical historical schema, and the small types a read is described in.

This file answers open question 3 -- *"the concrete column set implied by the
normalisation line"* -- for the two tables the EOD sources can fill. It is the
consumer's contract: **whichever vendor answered, a read returns these columns,
with these names, in these units.**

## The natural key answers open question 1, for history

A historical row is identified by **what the contract is, not by what a vendor
calls it**:

    equity_eod   (date, symbol)
    option_eod   (date, symbol, expiration, strike, right)

That tuple is the OCC identity. Every options vendor can express it, an IBKR
`conId` resolves *to* it, and it needs no cross-source id map to exist first --
which is why `history` can be built while open question 1 is still open. The
vendor's own identifiers are not thrown away: they stay in `raw/`, untouched,
where a future identity map can be built from them. What is settled here is
narrower and sufficient: *within a table, two rows are the same row when this
tuple matches.*

Two consequences worth stating out loud:

- **`strike` is in dollars and `right` is `"C"`/`"P"`.** ThetaData already says
  `160.0` and `"CALL"`; a vendor quoting strikes in thousandths gets divided in
  its projection, not here. Mixing conventions in one column is the kind of bug
  that produces a plausible backtest.
- **`symbol` is the underlying root**, not a contract symbol. `option_eod` rows
  for AAPL all carry `symbol='AAPL'`; the contract is the whole key.

## Provenance is part of the schema, not a debugging aid

Every table carries `source` and `ingested_at`. `ingested_at` is what makes a
vendor restatement a **new append** rather than an edit: reads default to the
newest append per natural key, and `scan()` still returns every version, which
is what lets a backtest ask what a source said about date D *as of* time T.
`source` is there because a table never mixes vendors -- see `store.read`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from antlia.schema import IDENTITY, Asset, Column, Dataset

#: How a read hands data back. `arrow` is what DuckDB produces and costs
#: nothing; the other two import a dataframe library only when asked for.
Frame = Literal["arrow", "pandas", "polars"]

#: Attached to every table. Not vendor data -- antlia's own bookkeeping about
#: where a row came from and when it arrived.
PROVENANCE: tuple[Column, ...] = (
    Column("source", "VARCHAR", "which adapter produced the row"),
    Column(
        "ingested_at",
        "TIMESTAMP WITH TIME ZONE",
        "when the append landed; restatements sort by it",
    ),
)

#: The pre-split name for `schema.Dataset`, kept so nothing that imported it
#: has to change. Every history table is a dataset with `PROVENANCE`.
TableSpec = Dataset


#: Shared by both EOD tables. ThetaData's EOD row is a session summary *plus*
#: the NBBO at the close stamp, and both halves are worth keeping: a backtest
#: that fills at `close` is not modelling anything, and one that fills at the
#: mid of `bid`/`ask` is.
_EOD_QUOTE: tuple[Column, ...] = (
    Column("open", "DOUBLE"),
    Column("high", "DOUBLE"),
    Column("low", "DOUBLE"),
    Column("close", "DOUBLE"),
    Column("volume", "BIGINT", "shares or contracts traded in the session"),
    Column("trades", "BIGINT", "number of trades; ThetaData calls this `count`"),
    Column("bid", "DOUBLE", "NBBO bid at the session's closing stamp"),
    Column("bid_size", "BIGINT"),
    Column("ask", "DOUBLE", "NBBO ask at the session's closing stamp"),
    Column("ask_size", "BIGINT"),
    Column("stamp", "TIMESTAMP WITH TIME ZONE", "the vendor's own stamp for the row, tz-aware"),
)

EQUITY_EOD = Dataset(
    name="equity_eod",
    asset=Asset.EQ,
    key=("date", "symbol"),
    columns=(
        Column("date", "DATE", "session date in the venue's own calendar"),
        *IDENTITY[Asset.EQ],
        *_EOD_QUOTE,
    ),
    doc="One row per symbol per session: OHLCV plus the closing NBBO.",
    provenance=PROVENANCE,
)

OPTION_EOD = Dataset(
    name="option_eod",
    asset=Asset.OPTION,
    key=("date", "symbol", "expiration", "strike", "right"),
    columns=(
        Column("date", "DATE", "session date"),
        *IDENTITY[Asset.OPTION],
        *_EOD_QUOTE,
    ),
    doc="One row per contract per session: OHLCV plus the closing NBBO.",
    provenance=PROVENANCE,
)

EXPIRATIONS = Dataset(
    name="expirations",
    asset=Asset.OPTION,
    key=("symbol", "expiration"),
    columns=(
        Column("symbol", "VARCHAR"),
        Column("expiration", "DATE"),
    ),
    doc="Every expiration a source has ever listed for a symbol.",
    provenance=PROVENANCE,
)

RATE_DAILY = Dataset(
    name="rate_daily",
    asset=Asset.RATE,
    key=("date", "series"),
    columns=(
        Column("date", "DATE", "the observation date the series assigns"),
        *IDENTITY[Asset.RATE],
        Column(
            "rate",
            "DOUBLE",
            "annualised, as a decimal (0.0422 is 4.22%), on the series' own basis",
        ),
    ),
    doc="One row per series per observation date: an interest rate.",
    provenance=PROVENANCE,
)

TABLES: dict[str, TableSpec] = {
    t.name: t for t in (EQUITY_EOD, OPTION_EOD, EXPIRATIONS, RATE_DAILY)
}

#: Tables laid out one directory per session date. `expirations` is not one of
#: them: it is a listing about a symbol, not an observation about a day.
DATED: frozenset[str] = frozenset({"equity_eod", "option_eod", "rate_daily"})


def table(name: str) -> TableSpec:
    if name not in TABLES:
        raise KeyError(f"unknown table {name!r}; known: {', '.join(sorted(TABLES))}")
    return TABLES[name]


@dataclass(frozen=True, slots=True)
class Window:
    """A closed date range. Both ends are included, as a trader would read it."""

    start: dt.date
    end: dt.date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"window ends before it starts: {self.start} .. {self.end}")

    @property
    def days(self) -> int:
        """Calendar days spanned, inclusive."""
        return (self.end - self.start).days + 1

    def clamp(self, start: dt.date | None = None, end: dt.date | None = None) -> Window | None:
        """This window narrowed to fit inside another. None when nothing is left."""
        lo = max(self.start, start) if start else self.start
        hi = min(self.end, end) if end else self.end
        return Window(lo, hi) if lo <= hi else None

    def chunks(self, days: int) -> list[Window]:
        """Split into pieces spanning at most `days` calendar days each.

        Vendors cap the span of one request -- ThetaData at 365 days -- and a
        request one day over the cap fails outright rather than being trimmed.
        """
        if days < 1:
            raise ValueError("chunk size must be at least one day")
        out: list[Window] = []
        cursor = self.start
        while cursor <= self.end:
            stop = min(cursor + dt.timedelta(days=days - 1), self.end)
            out.append(Window(cursor, stop))
            cursor = stop + dt.timedelta(days=1)
        return out

    def __contains__(self, day: object) -> bool:
        return isinstance(day, dt.date) and self.start <= day <= self.end

    def __str__(self) -> str:
        return f"{self.start} .. {self.end}"


def as_date(value: str | dt.date | dt.datetime) -> dt.date:
    """A date from whatever the caller had to hand.

    Consumers write dates as strings far more often than as `datetime.date`,
    and making every call site import `datetime` buys nothing.
    """
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def window(start: str | dt.date, end: str | dt.date) -> Window:
    return Window(as_date(start), as_date(end))


def merge(windows: Iterable[Window]) -> list[Window]:
    """Overlapping and adjacent windows collapsed into the fewest that cover them.

    Adjacent counts: 1st..3rd and 4th..6th become 1st..6th, because a gap of
    zero days is not a gap.
    """
    ordered = sorted(windows, key=lambda w: (w.start, w.end))
    out: list[Window] = []
    for w in ordered:
        if out and w.start <= out[-1].end + dt.timedelta(days=1):
            if w.end > out[-1].end:
                out[-1] = Window(out[-1].start, w.end)
        else:
            out.append(w)
    return out


def subtract(whole: Window, covered: Sequence[Window]) -> list[Window]:
    """What is left of `whole` once every `covered` window is removed.

    This is the fetch plan: the store says what it already holds, and the
    difference is exactly what a source has to be asked for. Everything else
    costs nothing and, on a metered key, that is the whole game.
    """
    gaps = [whole]
    for block in merge(covered):
        nxt: list[Window] = []
        for gap in gaps:
            if block.end < gap.start or block.start > gap.end:
                nxt.append(gap)
                continue
            if block.start > gap.start:
                nxt.append(Window(gap.start, block.start - dt.timedelta(days=1)))
            if block.end < gap.end:
                nxt.append(Window(block.end + dt.timedelta(days=1), gap.end))
        gaps = nxt
    return gaps


#: Tables whose plan is divided by the expiration listing, and whose coverage
#: is therefore only as trustworthy as that listing's age.
LISTED: frozenset[str] = frozenset({"option_eod"})


@dataclass(frozen=True, slots=True)
class Coverage:
    """What the store holds for one table and symbol, and what it does not.

    Deliberately about **completeness, not quality**: it answers "is every day
    in this window accounted for", never "are these prices any good". The
    second question is a judgement call and belongs to the consumer.

    `missing` is what was never fetched. `denied` is what a source refused --
    on a metered key the two are very different, and collapsing them makes the
    ingest path retry a window that will never be served.

    `listed_at` is when the expiration listing this plan divides work by was
    fetched, and it is the difference between an honest answer and a confident
    wrong one -- see `stale_listing`.
    """

    table: str
    symbol: str
    source: str
    requested: Window | None
    held: list[Window]
    missing: list[Window]
    denied: list[Window]
    rows: int
    #: When the expiration listing was last fetched. None for tables that have
    #: none, and None for an option table whose listing was never fetched.
    listed_at: dt.date | None = None

    @property
    def stale_listing(self) -> bool:
        """Whether the listing is too old to vouch for this window.

        **An expiration is always listed before it trades**, and the vendor's
        listing is historical -- everything ever listed for the symbol. So a
        listing fetched at `listed_at` knows every expiration that traded up to
        that date, and the test is exact rather than a guess at a TTL:

            listed_at >= requested.end   ->  the listing covers the window
            listed_at <  requested.end   ->  expirations may have been added since

        A TTL would be wrong in both directions: it would call a store warmed
        last night stale this morning, and it would say nothing about a store
        warmed in March being asked about September.
        """
        if self.table not in LISTED or self.requested is None:
            return False
        return self.listed_at is None or self.listed_at < self.requested.end

    @property
    def complete(self) -> bool:
        """Every day accounted for, **and** the listing old enough to say so.

        A stale listing makes this False even with nothing in `missing`,
        because `missing` is computed from that listing: an expiration it does
        not know about cannot appear as a gap.
        """
        return not self.missing and not self.stale_listing

    def __str__(self) -> str:
        span = f" for {self.requested}" if self.requested else ""
        head = f"{self.symbol} {self.table} via {self.source}{span}: {self.rows} rows"
        if self.complete and not self.denied:
            return f"{head}, complete"
        parts = []
        if self.missing:
            parts.append("missing " + ", ".join(str(w) for w in self.missing))
        if self.denied:
            parts.append("denied by source " + ", ".join(str(w) for w in self.denied))
        if self.stale_listing:
            # Said separately from `missing`, because it is a different
            # problem: not "these days are absent" but "we cannot tell".
            when = f"last fetched {self.listed_at}" if self.listed_at else "never fetched"
            parts.append(
                f"cannot vouch for this window -- the expiration listing was {when}, "
                "so expirations listed since are unknown; re-run fill to refresh it"
            )
        return f"{head}; " + "; ".join(parts)
