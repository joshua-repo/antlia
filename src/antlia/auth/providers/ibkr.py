"""Interactive Brokers via TWS / IB Gateway.

IBKR is not an API-key source. The gateway is already logged in; what antlia
resolves is *where it is listening*, and what it manages is the socket session
and the client id. Those are the two things that actually go wrong: a stale
connection, and two parts of a program picking the same client id and silently
evicting each other.

**Connections are read-only by default.** `readonly=True` makes the gateway
reject order placement on this socket, which turns antlia's "no execution path
here" rule into something the broker enforces rather than something a reviewer
has to notice. Setting it false is possible and is a deliberate act.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from typing import Any

from antlia.auth.base import Provider
from antlia.auth.credentials import Credential
from antlia.auth.errors import ConnectionFailed
from antlia.auth.ratelimit import Limiter
from antlia.auth.spec import Field, SourceSpec

#: Default ports by profile. These are **TWS**'s. IB Gateway listens on 4001
#: (live) and 4002 (paper) instead -- a difference that presents as a connection
#: timeout and nothing else, so it is worth setting explicitly rather than
#: remembering. Override with `port` in the config or ANTLIA_IBKR_<PROFILE>_PORT.
PROFILE_PORTS = {"paper": 7497, "live": 7496}

#: For reference in error messages and the config template.
GATEWAY_PORTS = {"paper": 4002, "live": 4001}

#: Client ids are allocated from a high base to stay clear of hand-run scripts
#: (ib_insync examples use 1..10, pictor uses 7). Cross-process collisions are
#: not solvable here -- give a second process its own base via
#: ANTLIA_IBKR_BASE_CLIENT_ID.
_client_ids = itertools.count()


class IBKRProvider(Provider):
    spec = SourceSpec(
        name="ibkr",
        fields=(
            Field("host", required=False, default="127.0.0.1", doc="gateway address"),
            Field("port", required=False, cast=int, doc="7497 paper / 7496 live"),
            Field("client_id", required=False, cast=int, doc="fixed id; auto if unset"),
            Field("base_client_id", required=False, default=900, cast=int),
            Field("readonly", required=False, default=True, cast=bool),
            Field("timeout", required=False, default=10.0, cast=float),
            Field("rate_limit", required=False, cast=float, doc="calls/sec override"),
        ),
        extra="ibkr",
        package="ib_insync",
        profiles=True,
        default_profile="paper",
        # Not client_id: an auto-allocated id must not split one gateway into
        # two pooled sessions.
        identity=("host", "port", "readonly"),
        rate=45.0,
        burst=45,
        # ib_insync drives an event loop and assumes it owns it; two
        # threads on one IB object deadlock rather than race.
        thread_safe=False,
        doc="Interactive Brokers TWS / IB Gateway socket session.",
    )

    def port_for(self, cred: Credential) -> int:
        port = cred.get("port")
        if port is not None:
            return int(port)
        if cred.profile in PROFILE_PORTS:
            return PROFILE_PORTS[cred.profile]
        raise ConnectionFailed(
            "ibkr",
            f"no port for profile {cred.profile!r}; set ANTLIA_IBKR_PORT or use "
            f"profile {' / '.join(sorted(PROFILE_PORTS))}",
        )

    def client_id(self, cred: Credential) -> int:
        fixed = cred.get("client_id")
        if fixed is not None:
            return int(fixed)
        return int(cred.get("base_client_id", 900)) + next(_client_ids)

    def describe(self, cred: Credential) -> dict[str, Any]:
        described = cred.redacted()
        with contextlib.suppress(ConnectionFailed):
            described["port"] = self.port_for(cred)
        return described

    def connect(self, cred: Credential, limiter: Limiter) -> Any:
        ib_insync = self.require("ib_insync")
        ib = ib_insync.IB()
        host, port = cred.get("host", "127.0.0.1"), self.port_for(cred)
        try:
            ib.connect(
                host,
                port,
                clientId=self.client_id(cred),
                readonly=bool(cred.get("readonly", True)),
                timeout=float(cred.get("timeout", 10.0)),
            )
        except Exception as exc:
            hint = ""
            if port in PROFILE_PORTS.values():
                hint = f"; IB Gateway uses {GATEWAY_PORTS} rather than TWS's {PROFILE_PORTS}"
            # A *timeout* rather than a refusal usually means something is
            # listening but the gateway behind it is not up -- a containerised
            # gateway's port forwarder accepts the TCP connection while IBC is
            # still waiting on an unanswered two-factor prompt.
            waiting = isinstance(exc, TimeoutError | asyncio.TimeoutError)
            cause = (
                "something accepted the connection but no gateway answered -- "
                "is it still logging in, or waiting on an unanswered two-factor prompt?"
                if waiting
                else f"is TWS/Gateway running on {host}:{port} with the API enabled?"
            )
            raise ConnectionFailed("ibkr", f"{type(exc).__name__}: {exc} ({cause}{hint})") from exc
        return ib

    def verify(self, handle: Any) -> str:
        if not self.healthy(handle):
            raise ConnectionFailed("ibkr", "socket is not connected")
        try:
            accounts = list(handle.managedAccounts())
            server = handle.client.serverVersion()
        except Exception as exc:
            raise ConnectionFailed("ibkr", f"{type(exc).__name__}: {exc}") from exc
        if not accounts:
            raise ConnectionFailed(
                "ibkr", "connected but the gateway manages no accounts (still logging in?)"
            )
        return f"server v{server}, accounts {accounts}"

    def healthy(self, handle: Any) -> bool:
        try:
            return bool(handle.isConnected())
        except Exception:
            return False

    def disconnect(self, handle: Any) -> None:
        handle.disconnect()
