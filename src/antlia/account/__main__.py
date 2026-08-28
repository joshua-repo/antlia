"""`python -m antlia.account` -- print a live account snapshot.

    python -m antlia.account ibkr -p live --set port=4001
    python -m antlia.account ibkr --json

Admin plumbing, like `antlia.auth`'s doctor: a way to see what a source actually
returns without writing a script. It reads and prints; it never writes anything.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from datetime import date, datetime
from typing import Any

from antlia.account import registry, snapshot
from antlia.account.types import BASE, AccountSnapshot
from antlia.auth.errors import AuthError

MONEY = "{:>16,.2f}"


def money(value: float | None) -> str:
    return MONEY.format(value) if value is not None else "{:>16}".format("--")


def render(snap: AccountSnapshot) -> str:
    lines = [
        f"account   {snap.account}   source {snap.source}   base {snap.base_currency}",
        f"as of     {snap.as_of:%Y-%m-%d %H:%M:%S %Z}",
    ]

    if snap.margin:
        m = snap.margin
        lines += ["", f"MARGIN ({m.currency})"]
        for label, value in (
            ("net liquidation", m.net_liquidation),
            ("equity with loan", m.equity_with_loan),
            ("gross position value", m.gross_position_value),
            ("total cash", m.total_cash),
            ("buying power", m.buying_power),
            ("initial margin", m.initial_margin),
            ("maintenance margin", m.maintenance_margin),
            ("available funds", m.available_funds),
            ("excess liquidity", m.excess_liquidity),
            ("look-ahead maint.", m.look_ahead_maintenance_margin),
        ):
            lines.append(f"  {label:<24}{money(value)}")
        cushion = f"{m.cushion:.3f}" if m.cushion is not None else "--"
        used = f"{m.utilisation:.1%}" if m.utilisation is not None else "--"
        leverage = f"{m.leverage:.2f}" if m.leverage is not None else "--"
        lines.append(f"  {'cushion / used / lev':<24}{cushion:>16}  {used}  {leverage}x")

    if snap.balances:
        lines += [
            "",
            f"BALANCES ({len(snap.balances)})",
            f"  {'ccy':<6}{'cash':>16}{'net liq':>18}{'unreal':>16}{'real':>14}   fx",
        ]
        for b in sorted(snap.balances, key=lambda x: (x.currency != BASE, x.currency)):
            mark = "*" if b.is_consolidated else " "
            fx = f"{b.exchange_rate:.6g}" if b.exchange_rate is not None else "--"
            lines.append(
                f" {mark}{b.currency:<5}{money(b.cash)}{money(b.net_liquidation):>18}"
                f"{money(b.unrealized_pnl)}{money(b.realized_pnl):>14}   {fx}"
            )
        lines.append("  * consolidated rollup -- excluded when summing")

    if snap.positions:
        kinds: dict[str, int] = {}
        for p in snap.positions:
            kinds[p.instrument.kind] = kinds.get(p.instrument.kind, 0) + 1
        summary = "  ".join(f"{k} {v}" for k, v in sorted(kinds.items()))
        lines += ["", f"POSITIONS ({len(snap.positions)})   {summary}"]
        lines.append(
            f"  {'instrument':<30}{'qty':>9}{'avg px':>13}{'mkt px':>13}"
            f"{'mkt value':>16}{'unreal':>14}"
        )
        for p in sorted(snap.positions, key=lambda x: (x.instrument.kind, str(x.instrument))):
            lines.append(
                f"  {str(p.instrument):<30}{p.quantity:>9,.0f}"
                f"{_px(p.average_price):>13}{_px(p.market_price):>13}"
                f"{money(p.market_value)}{money(p.unrealized_pnl):>14}"
            )

    if snap.orders:
        lines += ["", f"OPEN ORDERS ({len(snap.orders)})"]
        for o in snap.orders:
            lines.append(
                f"  {o.side:<5}{o.quantity:>8,.0f}  {str(o.instrument):<30}"
                f"{o.order_type or '':<6}{_px(o.limit_price):>12}  {o.status or ''}"
                f"  filled {o.filled:g}"
            )

    if snap.fills:
        spans_days = len({f.time.date() for f in snap.fills}) > 1
        stamp = "%Y-%m-%d %H:%M" if spans_days else "%H:%M:%S"
        lines += ["", f"FILLS ({len(snap.fills)})"]
        for f in sorted(snap.fills, key=lambda x: x.time, reverse=True):
            lines.append(
                f"  {f.time:{stamp}}  {f.side:<5}{f.quantity:>8,.0f}  "
                f"{str(f.instrument):<26}{_px(f.price):>12}"
                f"  comm {_px(f.commission)}  rPnL {_px(f.realized_pnl)}"
            )

    return "\n".join(lines)


def _px(value: float | None) -> str:
    return f"{value:,.4f}".rstrip("0").rstrip(".") if value is not None else "--"


def _encode(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    raise TypeError(type(obj).__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m antlia.account", description=__doc__)
    parser.add_argument("source", nargs="?", default="ibkr", choices=registry.sources())
    parser.add_argument("-a", "--account", default=None, help="account id (default: the first)")
    parser.add_argument("-p", "--profile", default=None)
    parser.add_argument(
        "--set", action="append", default=[], metavar="FIELD=VALUE", help="override a credential"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable instead of a table")
    args = parser.parse_args(argv)

    overrides = dict(pair.split("=", 1) for pair in args.set)
    try:
        snap = snapshot(args.source, args.account, args.profile or None, **overrides)
    except AuthError as exc:
        print(f"FAILED  {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(dataclasses.asdict(snap), default=_encode, indent=2))
    else:
        print(render(snap))
    return 0


if __name__ == "__main__":
    sys.exit(main())
