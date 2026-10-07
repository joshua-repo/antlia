"""Where a source's gateway is, how to look at it, and how to restart it.

    from antlia import gateway

    info = gateway.describe("ibkr", "live")
    info.screen_url        # noVNC, password already filled in
    info.vnc_addr          # "127.0.0.1:5900", for a native viewer
    info.control           # did IBC's command server answer just now

    gateway.restart("ibkr", "live")      # -> IBC's own words

    gateway.describe("trading212")       # -> None: a REST API has no gateway

Some sources are an HTTP endpoint and a key. One is a **desktop application in
a container**, and when it is not logged in, every layer above it is dark. This
module is the small amount of knowledge needed to diagnose and fix that, in the
one place all the consumers already depend on -- it was three hardcoded
constants in a dashboard before, unreachable from anything else.

## What this layer is not

**It cannot log a gateway in, and no future version will.** IB Gateway has no
headless mode and no login API; the only way in is a Swing dialog, the only
thing that types into it is IBC, and IBC lives inside the container. So there
is no `TWS_PASSWORD` here and nowhere to put one. The IBKR settings antlia
resolves are `{host, port, client_id, readonly, timeout}` -- endpoints, not
secrets -- and that is correct rather than incomplete. Trading212's
`api_key`/`api_secret` look similar and are not: those are real API
credentials, they belong in `auth`, and the two cases must not be merged.

The one secret this layer does hold is the **VNC password**, which protects a
view of a screen. It is prefilled into `screen_url` because noVNC's own
credential dialog is an ordinary web password field that browser extensions
fight the user for, in every frame. That makes `screen_url` a string that must
never be persisted -- see `types.py`, where the redaction lives in the type
rather than in each caller's discipline.

## What a consumer gets

`describe()` returns `GatewayInfo` or `None`, and `None` is the whole protocol
for "this source has nothing to look at" -- no list of special cases needed
anywhere else. Every field of a `GatewayInfo` is independently optional,
because every part of a gateway is: a control channel that is switched off is
an ordinary answer (IBC ships it disabled), not a failure.

`restart()` raises `ControlUnavailable` -- a `ConnectionFailed`, so
`except ConnectionFailed` still covers it -- when the channel is unreachable.
The message is prose, and it is meant to be shown rather than matched on.

**No extras and no vendor SDK.** Like `auth`, this runs on the standard
library, so a consumer who only wants to know whether the gateway is up is not
made to install `ib_insync`.

The host-side configuration that puts those ports there lives in
`ops/ib-gateway/` in this repo, with its own README.
"""

from __future__ import annotations

from antlia.gateway.base import RESTART, Gateway
from antlia.gateway.control import command, restart
from antlia.gateway.errors import ControlUnavailable
from antlia.gateway.ibc import PROBE_TIMEOUT, TIMEOUT
from antlia.gateway.reads import describe, gateways
from antlia.gateway.registry import register, unregister
from antlia.gateway.types import GatewayInfo, strip_password

__all__ = [
    # the two calls a consumer needs
    "describe",
    "restart",
    # the rest of the surface
    "command",
    "gateways",
    "GatewayInfo",
    "strip_password",
    # registry / extension
    "register",
    "unregister",
    "Gateway",
    # constants worth naming
    "RESTART",
    "TIMEOUT",
    "PROBE_TIMEOUT",
    # errors
    "ControlUnavailable",
]
