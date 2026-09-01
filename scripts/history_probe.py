"""Look at what the history store actually holds, then break holding a frame.

    python scripts/history_probe.py option_eod AAPL --start 2026-08-10 --end 2026-08-28
    python scripts/history_probe.py equity_eod AAPL --start 2026-08-17 --end 2026-08-28 --fetch

Meant for the launch configurations in `.vscode/launch.json`, and it answers the
two questions you actually have about a cache:

1. **Is it there?** It prints the store root, the files and bytes per table, and
   `coverage()` for the window -- which distinguishes *missing* (never fetched)
   from *denied* (the source refused and always will).
2. **Can I get it as an object?** It reads the window and breaks with the result
   bound three ways, so you can poke at whichever one your consumer uses:

       >>> table          # pyarrow.Table -- what a read returns
       >>> df             # pandas.DataFrame
       >>> pf             # polars.DataFrame
       >>> table.schema
       >>> df.groupby("expiration").size()

**It does not fetch unless you pass `--fetch`.** The point is to inspect the
cache, and a probe that silently spends a request on a metered plan would be
answering a different question than the one asked.

Breakpoints in `src/antlia/history/` hit on the way in; run with
`justMyCode: false` to step into DuckDB and pyarrow as well.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from antlia import auth, history  # noqa: E402
from antlia.auth.errors import AuthError  # noqa: E402
from antlia.history import ledger, store  # noqa: E402
from antlia.history.errors import HistoryError  # noqa: E402


def held(base: Path, source: str) -> None:
    """What is on disk for this source, per table."""
    for name in sorted(history.source(source).tables):
        files, size = store.size(base, source, name)
        symbols = ledger.symbols(base, source, name)
        if not files and not symbols:
            continue
        listed = ", ".join(symbols[:8]) + (" ..." if len(symbols) > 8 else "")
        print(f"held      {name:<12} {files:>5} files {size / 1024:>9.1f}KB  {listed}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("table", choices=["equity_eod", "option_eod"])
    parser.add_argument("symbol")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--source", default=None)
    parser.add_argument("--store", default=None)
    parser.add_argument("--expiration", default=None)
    parser.add_argument("--right", default=None, help="C/call or P/put")
    parser.add_argument("--dte", default=None, metavar="LOW,HIGH")
    parser.add_argument("--max-dte", type=int, default=None, help="shapes the plan, not the rows")
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="fill the gaps first (costs vendor requests); off by default",
    )
    parser.add_argument("--no-repl", dest="repl", action="store_false", help="skip the breakpoint")
    args = parser.parse_args(argv)

    base = history.path(args.store)
    source = args.source or history.chain()[0]
    print(f"store     {base}")
    print(f"source    {source}   (chain: {', '.join(history.chain())})")
    held(base, source)

    filters = {}
    if args.expiration:
        filters["expiration"] = args.expiration
    if args.right:
        filters["right"] = args.right
    if args.dte:
        low, _, high = args.dte.partition(",")
        filters["dte"] = (int(low), int(high))
    if args.max_dte is not None:
        filters["max_dte"] = args.max_dte

    try:
        # Coverage first, and always offline: it is the question "is this
        # cached", and answering it must not be what puts the data there.
        print(
            "coverage  "
            + str(
                history.coverage(
                    args.table,
                    args.symbol,
                    args.start,
                    args.end,
                    source=args.source,
                    store=args.store,
                    **{k: v for k, v in filters.items() if k == "max_dte"},
                )
            )
        )

        reader = history.option_eod if args.table == "option_eod" else history.equity_eod
        table = reader(
            args.symbol,
            args.start,
            args.end,
            source=args.source,
            store=args.store,
            fetch=args.fetch,
            **({} if args.table == "equity_eod" else filters),
        )
    except (AuthError, HistoryError) as exc:
        print(f"FAILED    {exc}")
        return 1
    finally:
        auth.close_all()

    print(f"read      {table.num_rows} rows x {table.num_columns} columns")
    print(f"columns   {', '.join(table.column_names)}")
    if table.num_rows:
        print(table.slice(0, 5))

    # The same rows in the shapes a consumer might want them in. Converted here
    # rather than left to the reader so the debugger has all three to hand.
    df = table.to_pandas()  # noqa: F841 -- bound for the debugger, not for this function
    pf = None  # noqa: F841
    try:
        import polars

        pf = polars.from_arrow(table)  # noqa: F841
    except ImportError:
        print("note      polars is not installed; `pf` is None")

    if args.repl:
        # `table` is the arrow table, `df` the pandas view, `pf` the polars one.
        breakpoint()
    return 0


if __name__ == "__main__":
    sys.exit(main())
