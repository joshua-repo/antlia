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


def test_fx_imports_auth_and_nothing_else_from_antlia():
    # The dependency order is auth <- fx. fx must not reach for history.
    out = run(
        "import antlia.fx, sys;"
        "print(sorted({m for m in sys.modules if m.startswith('antlia.')"
        " and not m.startswith(('antlia.auth', 'antlia.fx'))}))"
    ).stdout
    assert out.strip() == "[]"


def test_importing_fx_pulls_in_no_vendor_sdk():
    # The whole chain is lazy: the frankfurter fallback runs on the stdlib
    # alone, so `antlia[yfinance]` is a preference, not an install cost.
    out = run(
        "import sys;"
        "before = set(sys.modules);"
        "import antlia.fx;"
        "added = {m.split('.')[0] for m in set(sys.modules) - before};"
        "print(sorted(added & {'ib_insync', 'thetadata', 'yfinance', 'httpx', 'pandas'}))"
    ).stdout
    assert out.strip() == "[]"


def test_history_imports_auth_and_nothing_else_from_antlia():
    # The dependency order is auth, schema <- history. history must not reach
    # for fx or live: it is a read surface of its own, not a composition.
    out = run(
        "import antlia.history, sys;"
        "print(sorted({m for m in sys.modules if m.startswith('antlia.')"
        " and not m.startswith(('antlia.auth', 'antlia.history', 'antlia.schema'))}))"
    ).stdout
    assert out.strip() == "[]"


def test_schema_is_declarations_alone():
    # Every layer reads the schema, so it may import nothing from antlia and
    # nothing outside the standard library -- or it becomes everyone's cost.
    out = run(
        "import sys;"
        "before = set(sys.modules);"
        "import antlia.schema;"
        "added = set(sys.modules) - before;"
        "std = sys.stdlib_module_names;"
        "print(sorted({m for m in added if m != 'antlia.schema' and not m.startswith('_')"
        " and (m.startswith('antlia.') or m.split('.')[0] not in std)} - {'antlia'}))"
    ).stdout
    assert out.strip() == "[]"


def test_importing_history_pulls_in_no_vendor_sdk_and_no_dataframe_library():
    # duckdb and pyarrow are this layer's own machinery and load with it. A
    # consumer's dataframe preference is not: `frame="pandas"` imports pandas
    # when it is asked for, and never before.
    out = run(
        "import sys;"
        "before = set(sys.modules);"
        "import antlia.history;"
        "added = {m.split('.')[0] for m in set(sys.modules) - before};"
        "print(sorted(added & {'ib_insync', 'thetadata', 'yfinance', 'httpx', "
        "'pandas', 'polars'}))"
    ).stdout
    assert out.strip() == "[]"


def test_a_history_read_of_a_covered_window_never_authenticates():
    # The cache-first guarantee, enforced rather than documented: a covered
    # read must not so much as resolve a credential, or a backtest on a
    # metered plan silently costs a request per run.
    out = run(
        "import datetime as dt, tempfile, antlia.auth.pool as pool;"
        "from antlia import history;"
        "from tests.history.fakes import FakeHistory;"
        "src = FakeHistory();"
        "history.register('fake', src, first=True);"
        "root = tempfile.mkdtemp();"
        "history.equity_eod('AAPL', '2026-01-05', '2026-01-09', store=root);"
        "src.calls.clear();"
        "history.equity_eod('AAPL', '2026-01-05', '2026-01-09', store=root);"
        "print(src.calls, pool.open_sessions())"
    ).stdout
    assert out.strip() == "[] []"


def test_gateway_imports_auth_and_nothing_else_from_antlia():
    # The dependency order is auth <- gateway. It resolves endpoints through
    # auth's credential machinery and must not reach for a data layer: knowing
    # where a gateway is has nothing to do with what it serves.
    out = run(
        "import antlia.gateway, sys;"
        "print(sorted({m for m in sys.modules if m.startswith('antlia.')"
        " and not m.startswith(('antlia.auth', 'antlia.gateway'))}))"
    ).stdout
    assert out.strip() == "[]"


def test_gateway_needs_no_third_party_package():
    # The point of this layer is that it works when the broker's SDK cannot
    # connect -- so it must not need that SDK, or anything else, installed.
    out = run(
        "import sys;"
        "before = set(sys.modules);"
        "import antlia.gateway;"
        "from antlia import gateway;"
        "gateway.describe('ibkr', 'live');"
        "added = set(sys.modules) - before;"
        "std = sys.stdlib_module_names;"
        "print(sorted({m.split('.')[0] for m in added "
        "if not m.startswith('_') and m.split('.')[0] not in std "
        "and not m.startswith('antlia')}))"
    ).stdout
    assert out.strip() == "[]"


def test_describing_a_gateway_never_opens_a_broker_session():
    # A gateway is described precisely when the broker connection is in doubt.
    # If describing it authenticated, the diagnostic would fail exactly when it
    # is needed.
    out = run(
        "import antlia.auth.pool as pool;"
        "from antlia import gateway;"
        "gateway.describe('ibkr', 'live');"
        "gateway.describe('trading212', 'live');"
        "print(pool.open_sessions())"
    ).stdout
    assert out.strip() == "[]"
