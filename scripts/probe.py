"""Open one source's session and stop in the debugger holding it.

    python scripts/probe.py ibkr --profile paper
    python scripts/probe.py trading212 --no-repl

Meant for the launch configurations in `.vscode/launch.json`: it does what the
doctor does for a single source, then breaks with the live client bound to
`handle`, so it can be poked at in the Debug Console instead of guessed at.

    >>> handle.managedAccounts()
    >>> handle.get("/api/v0/equity/account/info").json()

Set breakpoints inside `antlia/auth/providers/` and they will hit on the way in;
run with `justMyCode: false` to step into the vendor SDK too.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from antlia import auth  # noqa: E402
from antlia.auth.errors import AuthError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=auth.sources())
    parser.add_argument("-p", "--profile", default=None)
    parser.add_argument("--verify", action="store_true", default=True)
    parser.add_argument("--no-verify", dest="verify", action="store_false")
    parser.add_argument("--no-repl", dest="repl", action="store_false", help="skip the breakpoint")
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="FIELD=VALUE",
        help="override a resolved field, e.g. --set port=4002",
    )
    args = parser.parse_args(argv)

    # An empty --profile (the pickString prompt with nothing typed) means
    # "whatever the spec defaults to", not a profile literally named "".
    profile = args.profile or None
    overrides = dict(pair.split("=", 1) for pair in args.set)
    provider = auth.provider(args.source)

    print(f"config    {auth.config_path()}")
    try:
        cred = auth.credential(args.source, profile, **overrides)
    except AuthError as exc:
        print(f"resolve   FAILED\n{exc}")
        return 1
    print(f"resolve   {provider.describe(cred)}")
    print(f"limiter   {auth.limiter(args.source, cred.profile)}")

    try:
        with auth.session(args.source, profile, **overrides) as handle:
            print(f"connect   {type(handle).__module__}.{type(handle).__name__}")
            if args.verify:
                print(f"verify    {provider.verify(handle)}")
            if args.repl:
                # `handle` is the live client. Inspect it here.
                breakpoint()
    except AuthError as exc:
        print(f"FAILED    {exc}")
        return 1
    finally:
        auth.close(args.source, profile)
    return 0


if __name__ == "__main__":
    sys.exit(main())
