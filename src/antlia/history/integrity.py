"""Is the store telling the truth about itself?

**This is integrity, not quality.** The distinction is the one CLAUDE.md draws
and it is load-bearing: whether a bid is crossed or a mid sits below intrinsic
is a *judgement*, and baking one researcher's rule into everybody's data is
exactly what this layer refuses to do. Whether a Parquet file is readable,
whether the ledger's account of what was fetched matches what is on disk, and
whether a killed process left rubbish behind are *facts*, and they are antlia's
problem because nothing else can see them.

Three things go wrong, and they are found differently:

- **An unreadable file.** The worst of the three, because of how the reader
  works: it unions the whole glob to align schemas, so one truncated file makes
  *every* date in that table unreadable, not just its own. A store with one bad
  file is a store with no data. `store.write` stages and renames so this cannot
  happen going forward; this finds the damage from before it did, and from
  anything that corrupts a file underneath us.
- **A leaked staging directory.** A write killed between `mkdir` and the rename
  leaves one. Harmless -- it is outside the read glob -- but it is disk, and it
  is evidence of the kill.
- **An expiration listing older than the data beside it.** An option plan
  divides work by that listing, so a store holding sessions the listing has
  never seen was planned against a chain that did not yet exist. `coverage()`
  reports this per window; this finds it across the whole store, without being
  asked about a particular one.
- **A count mismatch.** The ledger says how many rows each request wrote; the
  store says how many it holds. They should agree exactly, and the direction of
  a disagreement says what happened: *store < ledger* means rows were recorded
  and then lost; *store > ledger* means rows were written and never recorded,
  which is what a `SIGTERM` between the write and the flush produces.

Only the first two can be repaired here. A mismatch is repaired by re-fetching
with `fill(refresh=True)`, because only the vendor knows what should be there.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from antlia.history import ledger, store
from antlia.history.engine import pq
from antlia.history.types import TABLES

UNREADABLE = "unreadable"
LEAKED = "leaked"
MISMATCH = "mismatch"
STALE = "stale"


@dataclass(frozen=True, slots=True)
class Problem:
    """One thing wrong with the store."""

    kind: str
    detail: str
    #: The file or directory to act on, when there is one.
    path: Path | None = None
    #: What a caller should run when `repair` cannot fix it itself.
    advice: str = ""

    @property
    def repairable(self) -> bool:
        return self.kind in {UNREADABLE, LEAKED}

    def __str__(self) -> str:
        tail = f"  -- {self.advice}" if self.advice else ""
        return f"{self.kind:<11} {self.detail}{tail}"


def _files(base: Path, source: str | None) -> list[Path]:
    """Every Parquet file a read could open. The ledger is never source-scoped."""
    raw = (base / "raw" / source) if source else (base / "raw")
    found = sorted(raw.rglob("*.parquet")) if raw.exists() else []
    return found + sorted(store.ledger_dir(base).glob("*.parquet"))


def check(base: Path, source: str | None = None) -> list[Problem]:
    """Everything wrong with the store, most damaging first."""
    found: list[Problem] = []

    for path in _files(base, source):
        try:
            # Reads the footer only, which is precisely the part a truncated
            # write is missing.
            pq.read_metadata(path)
        except Exception as exc:
            found.append(
                Problem(
                    UNREADABLE,
                    f"{path} ({type(exc).__name__})",
                    path,
                    "quarantine it with --repair, then re-fetch with fill --refresh",
                )
            )

    staging = store.staging_dir(base)
    if staging.exists():
        for leftover in sorted(p for p in staging.iterdir() if p.is_dir()):
            found.append(
                Problem(LEAKED, f"{leftover} (a write was killed before its rename)", leftover)
            )

    found.extend(_listings(base, source))
    found.extend(_counts(base, source))
    # Unreadable first: nothing else can be trusted while one is present.
    order = {UNREADABLE: 0, MISMATCH: 1, STALE: 2, LEAKED: 3}
    return sorted(found, key=lambda p: order.get(p.kind, 9))


def _listings(base: Path, source: str | None) -> list[Problem]:
    """Symbols whose option data is newer than the listing it was planned from.

    Not repairable here, and deliberately not repaired silently: refreshing the
    listing is a vendor call, and `check` does not make those.
    """
    out: list[Problem] = []
    for name in store.sources(base):
        if source and name != source:
            continue
        for symbol in ledger.symbols(base, name, "option_eod"):
            held = ledger.windows(base, name, "option_eod", symbol)
            newest = max((w.end for w in held), default=None)
            if newest is None:
                continue
            listed = ledger.listed_at(base, name, symbol)
            if listed is not None and listed >= newest:
                continue
            when = f"last fetched {listed}" if listed else "never fetched"
            out.append(
                Problem(
                    STALE,
                    f"{name}:option_eod {symbol}: sessions held through {newest}, "
                    f"but the expiration listing was {when}",
                    None,
                    f"refresh it: antlia-history fill option_eod {symbol} --start ... --end ...",
                )
            )
    return out


def _counts(base: Path, source: str | None) -> list[Problem]:
    """Ledger totals against store totals, per source, table and symbol."""
    from antlia.history import registry

    out: list[Problem] = []
    for name in store.sources(base):
        if source and name != source:
            continue
        try:
            adapter = registry.get(name)
        except Exception:
            # A store written by an adapter this install does not have is not
            # a defect; it just cannot be counted without the projection.
            continue
        for table_name, spec in TABLES.items():
            for symbol in ledger.symbols(base, name, table_name):
                claimed = ledger.rows_for(base, name, table_name, symbol)
                try:
                    held = store.read(
                        base,
                        name,
                        spec,
                        adapter.projection(table_name),
                        where=[f'"{spec.subject}" = ?'],
                        params=[symbol],
                        latest=False,
                    ).num_rows
                except Exception as exc:
                    out.append(Problem(UNREADABLE, f"{name}:{table_name} {symbol}: {exc}", None))
                    continue
                if held == claimed:
                    continue
                direction = "lost since it was recorded" if held < claimed else "never recorded"
                out.append(
                    Problem(
                        MISMATCH,
                        f"{name}:{table_name} {symbol}: store {held} rows, ledger {claimed} "
                        f"({direction})",
                        None,
                        f"re-fetch: antlia-history fill {table_name} {symbol} "
                        f"--start ... --end ... --refresh",
                    )
                )
    return out


def repair(base: Path, problems: list[Problem]) -> list[str]:
    """Fix what can be fixed here. Returns what was done, in order.

    An unreadable file is **quarantined, never deleted**: it is the only copy
    of whatever the vendor said, and a file this code cannot read is not
    necessarily a file nothing can. Moving it out of the glob is what unblocks
    reads; deciding it is worthless is not this function's call.
    """
    done: list[str] = []
    for problem in problems:
        if problem.path is None:
            continue
        if problem.kind == UNREADABLE:
            target = store.quarantine_dir(base) / problem.path.relative_to(base)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(problem.path), str(target))
            done.append(f"quarantined {problem.path} -> {target}")
        elif problem.kind == LEAKED:
            shutil.rmtree(problem.path, ignore_errors=True)
            done.append(f"removed {problem.path}")
    return done
