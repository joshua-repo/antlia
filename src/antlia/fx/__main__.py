"""`python -m antlia.fx` -- show the rate table, and prove each source answers.

    python -m antlia.fx                          # the table, cache or live
    python -m antlia.fx -c USD,GBP,JPY,HKD       # a different currency set
    python -m antlia.fx --fresh                  # ignore the cache
    python -m antlia.fx --verify                 # every source, live, in turn
    python -m antlia.fx --convert 83289.64 GBP USD
    python -m antlia.fx --json
    python -m antlia.fx --clear-cache

Admin plumbing, like `antlia.auth`'s doctor. The same
three-level distinction applies: that a source is *registered* says nothing
about whether it *answers*. `--verify` is what to debug against, and it is the
only mode that talks to every source rather than stopping at the first that
works -- which is how a silently dead fallback gets found before the primary
needs it.
"""

from __future__ import annotations

import argparse
import json
import sys

from antlia.auth.errors import AuthError
from antlia.fx import cache, rates, registry
from antlia.fx.types import DEFAULT_CURRENCIES, RateTable

OK, BAD = "ok", "--"


def render(table: RateTable) -> str:
    hours = table.age.total_seconds() / 3600
    lines = [
        f"base     {table.base}",
        f"source   {table.source}{'   STALE -- every live source failed' if table.stale else ''}",
        f"as of    {table.as_of:%Y-%m-%d %H:%M %Z}   ({hours:.1f}h ago)",
        "",
        f"  {'ccy':<6}{'per 1 ' + table.base:>16}{'per 1 ccy':>16}",
    ]
    for currency, rate in sorted(table.rates.items()):
        inverse = table.inverse(currency)
        lines.append(f"  {currency:<6}{rate:>16,.6f}{inverse if inverse else 0.0:>16,.6f}")
    lines += [
        "",
        "  'per 1 ccy' is the orientation brokers report -- compare it with",
        "  the broker's own exchange rate, never the other column.",
    ]
    return "\n".join(lines)


def verify(names: list[str], currencies: tuple[str, ...]) -> int:
    """Every source in the chain, live, whether or not the one before worked."""
    failures = 0
    print(f"chain    {' -> '.join(names)}\n")
    for name in names:
        try:
            table = registry.get(name).rates(currencies)
        except AuthError as exc:
            print(f"{BAD} {name}: {exc}")
            failures += 1
            continue
        except Exception as exc:
            print(f"{BAD} {name}: {type(exc).__name__}: {exc}")
            failures += 1
            continue
        quoted = "  ".join(f"{c} {r:.6g}" for c, r in sorted(table.rates.items()))
        print(f"{OK} {name}: {quoted}   as of {table.as_of:%Y-%m-%d %H:%M %Z}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m antlia.fx", description=__doc__)
    parser.add_argument(
        "-c",
        "--currencies",
        default=",".join(DEFAULT_CURRENCIES),
        help=f"comma-separated (default: {','.join(DEFAULT_CURRENCIES)})",
    )
    parser.add_argument("-s", "--source", default=None, help="pin one source instead of the chain")
    parser.add_argument("--fresh", action="store_true", help="ignore the cache and fetch")
    parser.add_argument(
        "--verify", action="store_true", help="fetch from every source, not just the first"
    )
    parser.add_argument(
        "--convert", nargs=3, metavar=("AMOUNT", "FROM", "TO"), help="restate one figure"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable instead of a table")
    parser.add_argument("--clear-cache", action="store_true", help="delete the cached table")
    args = parser.parse_args(argv)

    if args.clear_cache:
        cache.clear()
        print(f"removed {cache.path()}")
        return 0

    currencies = tuple(c.strip().upper() for c in args.currencies.split(",") if c.strip())
    if args.convert:
        currencies = tuple(
            dict.fromkeys((*currencies, args.convert[1].upper(), args.convert[2].upper()))
        )

    if args.verify:
        names = [args.source] if args.source else registry.chain()
        failures = verify(names, currencies)
        if failures:
            print(f"\n{failures} problem(s)")
        return 1 if failures else 0

    try:
        table = rates(currencies, source=args.source, use_cache=not args.fresh)
    except AuthError as exc:
        print(f"FAILED  {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "base": table.base,
                    "rates": dict(table.rates),
                    "as_of": table.as_of.isoformat(),
                    "source": table.source,
                    "stale": table.stale,
                },
                indent=2,
            )
        )
    else:
        print(render(table))

    if args.convert:
        amount, frm, to = float(args.convert[0]), args.convert[1].upper(), args.convert[2].upper()
        converted = table.convert(amount, frm, to)
        if converted is None:
            print(f"\n{amount:,.2f} {frm} -> {to}: no rate", file=sys.stderr)
            return 1
        print(f"\n{amount:,.2f} {frm} = {converted:,.2f} {to}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
