"""Asking where a gateway is. Nothing here changes anything.

The split between this file and `control.py` is the layer's own rule made
visible in the file listing: describing is free and repeatable, commanding is
neither. It is the same shape as `history`'s `reads.py` / `writes.py`.
"""

from __future__ import annotations

from antlia.gateway import registry
from antlia.gateway.types import GatewayInfo


def describe(source: str, profile: str | None = None) -> GatewayInfo | None:
    """What is behind `source`, or `None` if it has no gateway.

        info = gateway.describe("ibkr", "live")
        if info is None:
            ...          # a REST API: there is no screen to draw
        elif info.control:
            ...          # a restart button is worth offering

    `None` is the answer for every source whose credentials are real API keys
    -- Trading212 has nothing to look at and nothing to restart. A consumer
    branches on it rather than keeping its own list of which sources have one.

    Opens one short-lived TCP connection to test the control channel, and
    nothing else. It never authenticates to the vendor, never opens a session,
    and resolves no secret beyond the VNC password.

    Raises `UnknownSource` for a name no registry knows, so a typo cannot pass
    as "this source has no gateway".
    """
    gateway = registry.get(source)
    return None if gateway is None else gateway.describe(profile)


def gateways() -> list[str]:
    """Every source that has a gateway adapter, reachable or not."""
    return registry.gateways()
