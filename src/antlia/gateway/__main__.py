"""`python -m antlia.gateway` -- what antlia thinks is on the other side.

    python -m antlia.gateway                     # every source, every gateway
    python -m antlia.gateway -s ibkr -p live
    python -m antlia.gateway --restart           # send RESTART, print the reply
    python -m antlia.gateway --command RECONNECTDATA
    python -m antlia.gateway --json

Admin plumbing, like the other doctors, and the same three-level distinction
applies: that a gateway is *configured* says nothing about whether it
*answers*. `control: yes` is the only line here that cost a round-trip.

**The password is never printed.** Output goes through `GatewayInfo.redacted()`,
so a pasted terminal transcript or a redirected `--json` carries a URL that
asks for the password rather than one that contains it.
"""

from __future__ import annotations

import argparse
import json
import sys

from antlia.auth.errors import AuthError
from antlia.gateway import registry
from antlia.gateway.control import command as send_command
from antlia.gateway.reads import describe
from antlia.gateway.types import GatewayInfo

OK, BAD = "ok", "--"


def render(info: GatewayInfo) -> str:
    label = info.source if info.profile is None else f"{info.source}:{info.profile}"
    screen = info.safe_screen_url or "(not published)"
    if info.screen_prefilled:
        screen += "   [+password, filled in at use]"
    lines = [
        f"{label}",
        f"  screen   {screen}",
        f"  vnc      {info.vnc_addr or '(not published)'}",
        f"  control  {info.control_addr or '(not configured)'}"
        f"   {OK if info.control else BAD} {'answers' if info.control else 'no answer'}",
    ]
    if info.control_addr and not info.control:
        lines.append(
            "           IBC's command server ships disabled (CommandServerPort=0);"
            "\n           ops/ib-gateway/ib-gateway.override.yml switches it on."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m antlia.gateway", description=__doc__)
    parser.add_argument("-s", "--source", default=None, help="one source instead of all of them")
    parser.add_argument("-p", "--profile", default=None, help="which profile to describe")
    parser.add_argument("--restart", action="store_true", help="send RESTART (needs --source)")
    parser.add_argument("--command", default=None, help="send one command verbatim")
    parser.add_argument("--json", action="store_true", help="machine-readable instead of a table")
    args = parser.parse_args(argv)

    names = [args.source] if args.source else registry.gateways()

    text = "RESTART" if args.restart else args.command
    if text:
        if not args.source:
            print("--restart and --command need -s/--source", file=sys.stderr)
            return 2
        try:
            reply = send_command(args.source, text, args.profile)
        except AuthError as exc:
            print(f"FAILED  {exc}", file=sys.stderr)
            return 1
        print(reply)
        return 0

    described: list[GatewayInfo] = []
    failures = 0
    for name in names:
        try:
            info = describe(name, args.profile)
        except AuthError as exc:
            print(f"{BAD} {name}: {exc}", file=sys.stderr)
            failures += 1
            continue
        if info is None:
            if args.source:
                print(f"{name} has no gateway (it is a REST API; nothing to look at)")
            continue
        described.append(info)

    if args.json:
        print(json.dumps([info.redacted() for info in described], indent=2))
    else:
        print("\n\n".join(render(info) for info in described))

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
