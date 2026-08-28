"""`python -m antlia.auth` -- report what is configured, without connecting.

Admin plumbing, not a product surface. It exists because the alternative to a
doctor command is discovering a typo'd environment variable in the middle of a
backtest, three layers down, as a vendor error.

    python -m antlia.auth                      # what resolves, what is installed
    python -m antlia.auth --connect            # also open a real session
    python -m antlia.auth ibkr --verify        # ...and prove it works
    python -m antlia.auth ibkr -p live         # one profile only
    python -m antlia.auth --init               # write a commented credentials.toml

Three levels, and they are not the same claim. *Resolves* means the settings
were found. *Connects* means an object was constructed -- which for an HTTP
source proves nothing about the key. *Verifies* means one real round-trip came
back. Debug against `--verify`.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys

from antlia.auth import credentials, pool, registry
from antlia.auth.errors import AuthError

OK, BAD, MEH = "ok", "--", "??"

TEMPLATE = """\
# antlia credentials. Never commit this file; nothing reads it from a repo.
# Environment variables win over everything here:
#   ANTLIA_<SOURCE>_<PROFILE>_<FIELD>, then ANTLIA_<SOURCE>_<FIELD>.

[ibkr]
host = "127.0.0.1"
# readonly = true          # false permits order placement -- antlia never needs it

# Ports below are TWS's. IB Gateway listens on 4002 (paper) / 4001 (live) --
# using the wrong pair looks exactly like "the gateway is not running".
[ibkr.paper]
port = 7497

[ibkr.live]
port = 7496

[thetadata]
# One of these three, not all:
# api_key = ""             # preferred
# email = ""
# password = ""
# creds_file = ""
# rate_limit = 20          # calls/sec; set this to your plan, not to a guess

[trading212.live]
# api_key = ""

[trading212.demo]
# api_key = ""

[yfinance]
# rate_limit = 2
"""


def init(force: bool) -> int:
    """Write a starter credentials file with owner-only permissions."""
    path = credentials.config_path()
    if path.exists() and not force:
        print(f"{path} already exists; --init --force overwrites it")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE)
    path.chmod(0o600)
    print(f"wrote {path} (mode 600)")
    return 0


def _installed(package: str | None) -> bool:
    if not package:
        return True
    return importlib.util.find_spec(package) is not None


def report(
    names: list[str],
    connect: bool,
    verify: bool,
    only_profile: str | None = None,
) -> int:
    path = credentials.config_path()
    print(f"config   {path}{'' if path.exists() else '   (absent)'}")
    complaint = credentials.check_permissions(path)
    if complaint:
        print(f"WARNING  {complaint}")
    print()

    failures = 0
    for name in names:
        try:
            provider = registry.get(name)
        except AuthError as exc:
            print(f"{BAD} {name}: {exc}")
            failures += 1
            continue

        spec = provider.spec
        sdk = "installed" if _installed(spec.package) else f"missing (antlia[{spec.extra}])"
        configured: list[str | None] = list(credentials.profiles(name))
        if not configured:
            configured = [spec.default_profile] if spec.profiles else [None]
        if only_profile is not None:
            configured = [only_profile]
        for profile in configured:
            label = name if profile is None else f"{name}:{profile}"
            try:
                cred = pool.credential(name, profile)
            except AuthError as exc:
                print(f"{BAD} {label}: {exc}")
                failures += 1
                continue
            print(f"{OK} {label}: {provider.describe(cred)}   sdk {sdk}")

            if not (connect or verify):
                continue
            if not _installed(spec.package):
                print(f"   {MEH} skipped: SDK not installed")
                continue
            try:
                with pool.session(name, profile) as handle:
                    print(f"   {OK} connect: {type(handle).__module__}.{type(handle).__name__}")
                    if verify:
                        print(f"   {OK} verify: {provider.verify(handle)}")
            except AuthError as exc:
                print(f"   {BAD} {exc}")
                failures += 1
            finally:
                pool.close(name, profile)
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m antlia.auth", description=__doc__)
    parser.add_argument("sources", nargs="*", help="sources to check (default: all)")
    parser.add_argument(
        "--connect",
        action="store_true",
        help="actually open a session -- talks to the network / gateway",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="connect, then make one cheap real call to prove the credential works",
    )
    parser.add_argument(
        "-p", "--profile", help="check only this profile instead of every configured one"
    )
    parser.add_argument("--init", action="store_true", help="write a starter config file")
    parser.add_argument("--force", action="store_true", help="with --init, overwrite")
    args = parser.parse_args(argv)
    if args.init:
        return init(args.force)
    names = args.sources or registry.sources()
    failures = report(names, args.connect, args.verify, args.profile)
    if failures:
        print(f"\n{failures} problem(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
