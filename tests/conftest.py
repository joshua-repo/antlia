from __future__ import annotations

import pytest

from antlia.auth import pool, ratelimit, registry


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Every test gets an empty config dir, registry, pool and limiter table.

    All three are process-global by design (a rate limit belongs to the account,
    not the caller), so leaking them between tests would make failures depend on
    ordering.
    """
    monkeypatch.setenv("ANTLIA_HOME", str(tmp_path))
    for key in [k for k in dict(__import__("os").environ) if k.startswith("ANTLIA_")]:
        if key != "ANTLIA_HOME":
            monkeypatch.delenv(key, raising=False)
    pool.close_all()
    registry.reset()
    ratelimit.reset()
    yield
    pool.close_all()
    registry.reset()
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
