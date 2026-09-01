"""`antlia-history` -- the doctor, the planner, and the ingest command.

    antlia-history                             # what is registered, what is held
    antlia-history verify                      # one live round-trip per source
    antlia-history check [--repair]            # is the store telling the truth?
    antlia-history plan option_eod AAPL --start 2026-06-01 --end 2026-08-28
    antlia-history fill option_eod AAPL --start 2026-06-01 --end 2026-08-28 --workers 2
    antlia-history coverage option_eod AAPL --start 2026-06-01 --end 2026-08-28
    antlia-history read equity_eod AAPL --start 2026-08-17 --end 2026-08-28
    antlia-history ingests option_eod AAPL     # what --as-of accepts

`plan` is the one to run first on a metered key: it prints how many vendor
requests a `fill` would make, and costs none of them.

**There is no `__main__.py`.** `antlia-history` is a console script pointing at
`main` below, so the command lives in one place instead of being split between
a module that holds it and a three-line shim that exists only to satisfy
`python -m`. Where the entry point is not on PATH -- a source checkout with no
install -- `python -m antlia.history.cli` runs the same thing.

The commands are a table, and their flags are reusable groups, because the
alternative -- a handler plus its own block of `add_argument` calls per command
-- is where a CLI quietly grows nine slightly different spellings of
`--max-dte`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from antlia import auth, history
from antlia.auth.errors import AuthError
from antlia.history import ingest, ledger, store, types
from antlia.history.errors import HistoryError

Group = Callable[[argparse.ArgumentParser], None]


# -- reusable flag groups --------------------------------------------------


def target(parser: argparse.ArgumentParser) -> None:
    """Which table, and which symbols. `read` takes a universe in one frame."""
    parser.add_argument("table", choices=sorted(types.TABLES))
    parser.add_argument("symbol", nargs="+")


def dates(required: bool) -> Group:
    def add(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--start", required=required)
        parser.add_argument("--end", required=required)

    return add


def shape(parser: argparse.ArgumentParser) -> None:
    """Flags that change what gets *fetched*, not what is returned."""
    parser.add_argument("--expiration", action="append", help="repeatable; limits the plan")
    parser.add_argument("--max-dte", type=int, default=None)
    parser.add_argument("--min-dte", type=int, default=None)


def refresh(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="re-fetch even where the ledger already covers it, appending a new version",
    )


def spend(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, default=None, help="cap the vendor requests")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="requests in flight at once; clamped to the vendor's ceiling, "
        "and to 1 for a single-threaded SDK",
    )


def rows(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--rows", type=int, default=20)
    parser.add_argument("--every", action="store_true", help="every append, not the newest per key")
    parser.add_argument("--as-of", default=None, metavar="TIMESTAMP", help="hide later appends")


def repairable(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repair", action="store_true", help="quarantine bad files and clear leaked staging"
    )


def one_symbol(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("symbol", nargs="?", default="AAPL")


# -- what each command does ------------------------------------------------


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.0f}B"


def _plan_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    """The plan-shaping options, for the commands that take `shape`."""
    out: dict[str, Any] = {"source": args.source, "store": args.store}
    for flag in ("max_dte", "min_dte"):
        if getattr(args, flag, None) is not None:
            out[flag] = getattr(args, flag)
    if getattr(args, "expiration", None):
        out["expirations"] = args.expiration
    return out


def doctor(args: argparse.Namespace) -> int:
    base = history.path(args.store)
    print(f"store    {base}")
    print(f"chain    {', '.join(history.chain())}")
    listed = store.sources(base)
    for name in listed:
        known = history.source(name).tables if name in history.chain() else ()
        for table in sorted(known):
            files, size = store.size(base, name, table)
            found = ledger.symbols(base, name, table)
            if not files and not found:
                continue
            shown = ", ".join(found[:8]) + (" ..." if len(found) > 8 else "")
            print(f"  {name}:{table:<12} {files:>4} files  {_human(size):>8}  {shown}")
    if not listed:
        print("         (empty -- nothing has been ingested yet)")
    return 0


def verify(args: argparse.Namespace) -> int:
    # `--source` narrows it, which is what you want on a machine with a key for
    # one vendor and not the others: a doctor that always shows a red line for
    # a source you never configured stops being read.
    failed = 0
    for name in [args.source] if args.source else history.chain():
        try:
            print(f"ok {name}: {history.source(name).verify()}")
        except (AuthError, HistoryError) as exc:
            failed += 1
            print(f"FAILED {name}: {exc}")
    return 1 if failed else 0


def check(args: argparse.Namespace) -> int:
    problems = history.check(source=args.source, store=args.store, repair=args.repair)
    if not problems:
        print("ok       nothing wrong with the store")
        return 0
    for problem in problems:
        print(problem)
    if args.repair:
        left = [p for p in problems if not p.repairable]
        print(f"\nrepaired {len(problems) - len(left)}; {len(left)} need a re-fetch")
    else:
        print(f"\n{len(problems)} problems. Re-run with --repair to quarantine and clean up.")
    return 1


def horizon(args: argparse.Namespace) -> int:
    src: Any = history.source(args.source)
    measure = getattr(src, "measure_earliest", None)
    declared = src.earliest("equity_eod")
    if measure is None:
        print(f"{src.name} does not measure a horizon; it declares {declared}")
        return 0
    print(f"declared  {declared}")
    found = measure(args.symbol)
    print(f"measured  {found}   ({args.symbol})")
    if found != declared:
        print(f"\nThe window has moved. Set ANTLIA_THETADATA_EARLIEST={found}, or update")
        print("MEASURED_EARLIEST in antlia/history/sources/thetadata.py.")
    return 0


def plan(args: argparse.Namespace) -> int:
    for symbol in args.symbol:
        intent = history.plan(
            args.table, symbol, args.start, args.end, refresh=args.refresh, **_plan_kwargs(args)
        )
        print(intent)
        if args.verbose:
            for request in intent.requests:
                print(f"  {request}")
    return 0


def fill(args: argparse.Namespace) -> int:
    def progress(request: ingest.Request, entry: ledger.Entry) -> None:
        if args.verbose or entry.status == ledger.DENIED:
            print(f"  {entry}")

    # One symbol at a time, because a Report is about one symbol. The
    # parallelism that matters is inside a symbol's own request list.
    for symbol in args.symbol:
        print(
            history.fill(
                args.table,
                symbol,
                args.start,
                args.end,
                limit=args.limit,
                workers=args.workers,
                refresh=args.refresh,
                progress=progress,
                **_plan_kwargs(args),
            )
        )
    return 0


def coverage(args: argparse.Namespace) -> int:
    for symbol in args.symbol:
        print(history.coverage(args.table, symbol, args.start, args.end, **_plan_kwargs(args)))
    return 0


def read(args: argparse.Namespace) -> int:
    reader = history.option_eod if args.table == "option_eod" else history.equity_eod
    # The shape flags are passed through, and for options they matter: this
    # never fetches, so a window filled at `--max-dte 20` reads back only when
    # asked the same way. Without them the plan considers every expiration and
    # reports the window uncovered while the rows you want are sitting there.
    data = reader(
        args.symbol,
        args.start,
        args.end,
        fetch=False,
        latest=not args.every,
        as_of=dt.datetime.fromisoformat(args.as_of) if args.as_of else None,
        **_plan_kwargs(args),
    )
    print(data.slice(0, args.rows))
    print(f"{data.num_rows} rows")
    return 0


def ingests(args: argparse.Namespace) -> int:
    for symbol in args.symbol:
        stamps = history.ingests(args.table, symbol, source=args.source, store=args.store)
        print(f"{symbol} {args.table}: {len(stamps)} writes")
        shown = stamps if (args.verbose or len(stamps) <= 6) else [*stamps[:3], None, *stamps[-3:]]
        for stamp in shown:
            print(f"  {stamp.isoformat()}" if stamp else f"  ... {len(stamps) - 6} more (-v)")
    return 0


# -- the table -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    help: str
    run: Callable[[argparse.Namespace], int]
    groups: tuple[Group, ...] = field(default_factory=tuple)


COMMANDS: tuple[Command, ...] = (
    Command("doctor", "what is registered and what is held", doctor),
    Command("verify", "one live round-trip per source", verify),
    Command("check", "unreadable files, leaked writes, ledger mismatches", check, (repairable,)),
    Command("horizon", "measure how far back this plan may read", horizon, (one_symbol,)),
    Command(
        "plan",
        "what a fill would ask for, without asking",
        plan,
        (target, dates(True), shape, refresh),
    ),
    Command(
        "fill",
        "fetch and store what is missing",
        fill,
        (target, dates(True), shape, refresh, spend),
    ),
    Command(
        "coverage", "what is held, missing and denied", coverage, (target, dates(False), shape)
    ),
    Command("read", "print stored rows (never fetches)", read, (target, dates(True), shape, rows)),
    Command("ingests", "the moments --as-of can be given", ingests, (target, dates(False))),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="antlia-history", description=__doc__)
    parser.add_argument("--store", default=None, help="store root; default $ANTLIA_STORE")
    parser.add_argument("--source", default=None, help="which adapter; default the chain's first")
    parser.add_argument("-v", "--verbose", action="store_true")
    subs = parser.add_subparsers(dest="command")
    for command in COMMANDS:
        sub = subs.add_parser(command.name, help=command.help)
        for group in command.groups:
            group(sub)

    args = parser.parse_args(argv)
    chosen = {c.name: c.run for c in COMMANDS}
    run = chosen.get(args.command, doctor)
    try:
        return run(args)
    except (AuthError, HistoryError) as exc:
        print(f"FAILED   {exc}")
        return 1
    finally:
        auth.close_all()


if __name__ == "__main__":
    # `python -m antlia.history.cli`, for a checkout with no console script.
    sys.exit(main())
