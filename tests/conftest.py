from __future__ import annotations

import pytest

from antlia.auth import pool, ratelimit, registry
from antlia.fx import registry as fx_registry
from antlia.gateway import registry as gateway_registry
from antlia.history import registry as history_registry


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Every test gets an empty config dir, registries, pool and limiter table.

    `ANTLIA_HOME` also relocates the fx rate cache *and* the history store, so
    a test never reads the developer's own `~/.antlia/fx.json` or writes into
    their warmed Parquet tree.

    All five are process-global by design (a rate limit belongs to the account,
    not the caller), so leaking them between tests would make failures depend on
    ordering.
    """
    monkeypatch.setenv("ANTLIA_HOME", str(tmp_path))
    for key in [k for k in dict(__import__("os").environ) if k.startswith("ANTLIA_")]:
        if key != "ANTLIA_HOME":
            monkeypatch.delenv(key, raising=False)
    pool.close_all()
    registry.reset()
    fx_registry.reset()
    gateway_registry.reset()
    history_registry.reset()
    ratelimit.reset()
    yield
    pool.close_all()
    registry.reset()
    fx_registry.reset()
    gateway_registry.reset()
    history_registry.reset()
    ratelimit.reset()


@pytest.fixture
def config(tmp_path):
    """Write ~/.antlia/credentials.toml and return its path."""

    def write(text: str, mode: int = 0o600):
        path = tmp_path / "credentials.toml"
        path.write_text(text)
        path.chmod(mode)
        return path

    return write
