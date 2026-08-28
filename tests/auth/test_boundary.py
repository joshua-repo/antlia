"""The rules from CLAUDE.md that are cheap to enforce and expensive to lose."""

from __future__ import annotations

import subprocess
import sys


def run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)


def test_importing_antlia_pulls_in_no_subpackage():
    # `import antlia` must stay free of the query engine and of vendor SDKs.
    out = run(
        "import antlia, sys;print([m for m in sys.modules if m.startswith('antlia.')])"
    ).stdout
    assert out.strip() == "[]"


def test_auth_imports_nothing_else_from_antlia():
    # auth is the bottom layer: a consumer wanting only credentials must not be
    # made to install duckdb.
    out = run(
        "import antlia.auth, sys;"
        "print(sorted(m for m in sys.modules "
        "if m.startswith('antlia.') and not m.startswith('antlia.auth')))"
    ).stdout
    assert out.strip() == "[]"


def test_auth_needs_no_third_party_package():
    # Measured as modules *added* by the import, so whatever the interpreter
    # loaded at startup (sitecustomize and friends) is not attributed to us.
    out = run(
        "import sys;"
        "before = set(sys.modules);"
        "import antlia.auth;"
        "added = set(sys.modules) - before;"
        "std = sys.stdlib_module_names;"
        "print(sorted({m.split('.')[0] for m in added "
        "if not m.startswith('_') and m.split('.')[0] not in std "
        "and not m.startswith('antlia')}))"
    ).stdout
    assert out.strip() == "[]"


def test_account_imports_auth_and_nothing_else_from_antlia():
    # The dependency order is auth <- account. If account ever reaches for
    # history, the "three read surfaces, one write path" rule has been broken.
    out = run(
        "import antlia.account, sys;"
        "print(sorted({m for m in sys.modules if m.startswith('antlia.')"
        " and not m.startswith(('antlia.auth', 'antlia.account'))}))"
    ).stdout
    assert out.strip() == "[]"


def test_importing_account_pulls_in_no_broker_sdk():
    out = run(
        "import sys;"
        "before = set(sys.modules);"
        "import antlia.account;"
        "added = {m.split('.')[0] for m in set(sys.modules) - before};"
        "print(sorted(added & {'ib_insync', 'thetadata', 'yfinance', 'httpx'}))"
    ).stdout
    assert out.strip() == "[]"
