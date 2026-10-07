"""The gateway-adapter contract.

A `Gateway` answers two questions about one source's infrastructure: *where is
it and how do I look at it*, and *how do I tell it to restart*. It is
deliberately not a data path -- nothing here reads a position or a price, and
nothing here authenticates to the vendor.

**No credential of the vendor's belongs in this layer.** IB Gateway's login is
typed into a Swing dialog by IBC, inside the container; there is no API to call
and `TWS_PASSWORD` has nowhere to live in antlia. What this layer resolves is
endpoints -- host and three port numbers -- plus, for the screen, a VNC
password that protects a view of a screen and is not a broker credential. A
source whose credentials *are* real API keys (Trading212) has no gateway at
all, and `describe()` says so by returning nothing.

Settings resolve through `auth.credentials.resolve`, against the source's own
section: `[ibkr] control_port` and `ANTLIA_IBKR_LIVE_CONTROL_PORT` sit beside
the `host` and `port` `auth` already resolves from there. One broker, one
section -- the layering is antlia's business, not the reader's. The spec is
**not** registered with `auth.registry`, because there is no session to open:
`resolve()` is a pure function over a spec and needs no registration.
"""

from __future__ import annotations

import abc

from antlia.auth.credentials import Credential, resolve
from antlia.auth.spec import SourceSpec
from antlia.gateway.types import GatewayInfo

#: The command every gateway is expected to understand. A named view rather
#: than a free-text call, because it is the one action a consumer should reach
#: for: it recovers a wedged gateway and changes nothing else.
RESTART = "RESTART"


class Gateway(abc.ABC):
    """The infrastructure behind one source."""

    #: The antlia source name this gateway belongs to.
    name: str
    #: How its endpoints are configured. Resolved, never registered.
    spec: SourceSpec

    def credential(self, profile: str | None = None) -> Credential:
        """Resolved endpoints for one profile.

        Every field is optional with a default, so this cannot raise
        `MissingCredential` -- `describe()` answering is not contingent on
        anything having been configured.
        """
        return resolve(self.spec, profile)

    @abc.abstractmethod
    def describe(self, profile: str | None = None) -> GatewayInfo:
        """Where this gateway is, and whether its control channel answers.

        Cheap and side-effect free apart from one liveness probe. It must not
        raise for an absent or disabled component: "there is no screen" and
        "the control channel is down" are answers, carried in the fields.
        """

    @abc.abstractmethod
    def command(self, text: str, profile: str | None = None) -> str:
        """Send one command and return the reply verbatim.

        Raises `ControlUnavailable` when the channel cannot be reached. The
        reply is prose for a person; adapters do not parse it and neither
        should consumers.
        """

    def restart(self, profile: str | None = None) -> str:
        """Ask the gateway to restart itself. Returns what it said."""
        return self.command(RESTART, profile)
