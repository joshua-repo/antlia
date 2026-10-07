"""What a row means: asset classes, their identity, and the dataset contract.

antlia is laid out on two axes, and they decide different things:

- **Access** -- `history` and `live` -- decides what a read *promises*.
  History is fixed: given `as_of`, a read returns the same rows forever.
  Live is a function of the moment you ask, stamped, and never stored.
- **Asset class** -- the `Asset`s below -- decides what a row *is*: which
  columns identify the instrument, and in what units.

The second axis is shared. A live option quote and a historical option row
carry the same identity columns, so a consumer can join today's snapshot onto
yesterday's history without a mapping table, and a backtest and a forward run
can feed one engine. That is why these declarations live here rather than in
either access package: neither owns them.

Declarations only. No I/O, no third-party import -- `auth`, `history` and
`live` all read this file, and it must not cost any of them an install.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Asset(StrEnum):
    """The asset classes antlia serves. A new one is a row here, not a package."""

    EQ = "eq"
    OPTION = "option"
    FX = "fx"
    RATE = "rate"


#: Canonical option rights. One letter, because that is what OCC symbols,
#: IBKR and every vendor's own docs agree on once the spelling is stripped.
CALL = "C"
PUT = "P"


@dataclass(frozen=True, slots=True)
class Column:
    """One canonical column: its name, its DuckDB type, and what it means."""

    name: str
    type: str
    doc: str = ""


#: The columns that say *which instrument* a row is about, per asset class.
#: Every dataset of that class carries them, under these names, in these
#: units -- in `history` and in `live` alike.
IDENTITY: dict[Asset, tuple[Column, ...]] = {
    Asset.EQ: (Column("symbol", "VARCHAR"),),
    Asset.OPTION: (
        Column("symbol", "VARCHAR", "underlying root, not a contract symbol"),
        Column("expiration", "DATE"),
        Column("strike", "DOUBLE", "in dollars, never thousandths"),
        Column("right", "VARCHAR", "'C' or 'P'"),
    ),
    Asset.FX: (Column("currency", "VARCHAR", "ISO code; quoted as units per 1 USD"),),
    Asset.RATE: (Column("series", "VARCHAR", "antlia's series id, e.g. 'UST_3M'"),),
}


@dataclass(frozen=True, slots=True)
class Dataset:
    """A canonical dataset: its asset class, its columns, what makes a row unique.

    `key` is the natural key. It is the whole of what "the same row" means: in
    `history`, a later append with a matching key **restates** the earlier one
    and wins a default read, rather than duplicating it.

    `provenance` is antlia's own bookkeeping about where a row came from --
    `source` and `ingested_at` for history -- and is kept apart from `columns`
    because a vendor's projection supplies the one and the store the other.
    """

    name: str
    asset: Asset
    key: tuple[str, ...]
    columns: tuple[Column, ...]
    doc: str = ""
    provenance: tuple[Column, ...] = ()

    @property
    def all_columns(self) -> tuple[Column, ...]:
        return (*self.columns, *self.provenance)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.all_columns)
