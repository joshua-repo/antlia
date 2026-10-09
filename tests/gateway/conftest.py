from __future__ import annotations

import pytest

from antlia.gateway.sources.ibkr import VNC_PASSWORD_ENV


@pytest.fixture(autouse=True)
def no_ambient_vnc_password(monkeypatch):
    """The one setting this layer reads that is not `ANTLIA_`-prefixed.

    The shared `isolated_state` fixture clears `ANTLIA_*`; this variable is
    deliberately the gateway project's own spelling, so a developer who has
    sourced `~/ib-gateway/.env` in their shell would otherwise run these tests
    against a real password.
    """
    monkeypatch.delenv(VNC_PASSWORD_ENV, raising=False)
