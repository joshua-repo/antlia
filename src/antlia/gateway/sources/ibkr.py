"""IB Gateway, as seen from outside the container.

The layering this adapter exists to serve, and which nothing here may blur:

    IBC (in the container) --drives the Swing login dialog--> IB Gateway
                                 --and only once logged in--> :4001
                                                                |
                                                             antlia.auth
                                                                |
                                                             consumers

**IB Gateway has no headless mode and no login API.** The only way in is that
GUI dialog, the only thing that can type into it is IBC, and IBC must be inside
the container. So antlia does not, and will never, log a gateway in: there is
no interface to call and `TWS_PASSWORD` has nowhere to live here. What this
module offers instead is the two things that *are* reachable from outside --
a way to look at the screen, and a way to ask IBC to restart the gateway.

Three endpoints, published next to the API port by
`ops/ib-gateway/ib-gateway.override.yml`:

- **the noVNC bridge** (6080), a web page showing the gateway's own screen.
  VNC is RFB over raw TCP and browser JS cannot open a TCP socket, so a
  websockify sidecar translates. This is what makes a two-factor prompt
  answerable from a browser -- and it is why no 2FA *detection* is needed
  anywhere: a human reading the screen is the detector.
- **the VNC server itself** (5900), for a native viewer. noVNC does not use
  the Keyboard Lock API, so the browser -- and every extension injected into
  the frame -- sees keystrokes before the remote screen does. That is a
  property of the browser rather than of the web client, so the escape hatch
  is to take the browser out of the path.
- **IBC's command server** (7462), off in a stock gateway. See `ibc.py`.

Settings live in the source's own config section, beside the ones `auth`
resolves:

    [ibkr]
    host = "127.0.0.1"
    control_port = 7462

    [ibkr.live]
    screen_port = 6080

    $ANTLIA_IBKR_LIVE_CONTROL_PORT=7462

`host` is deliberately the same field `auth` reads, so moving the gateway to
another machine is one edit rather than four. Any port set to `0` means "not
published", and the corresponding field comes back `None` rather than pointing
at something that is not there.
"""

from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from antlia.auth.credentials import Credential
from antlia.auth.spec import Field, SourceSpec
from antlia.gateway import ibc
from antlia.gateway.base import Gateway
from antlia.gateway.errors import ControlUnavailable
from antlia.gateway.types import PASSWORD_PARAM, GatewayInfo

#: The page websockify serves from `/usr/share/novnc`.
SCREEN_PATH = "/vnc.html"

#: `autoconnect=1` is what stops noVNC drawing its credential dialog once a
#: password is supplied; `resize=scale` fits the gateway's window to whatever
#: the panel gives it. Neither is a preference a consumer should have to know.
SCREEN_PARAMS = (("autoconnect", "1"), ("resize", "scale"))

#: The plain environment variable the VNC password is also read from, named to
#: match the gateway compose project's own. It is the *last* place consulted,
#: under antlia's own spellings, and it exists so that
#:
#:     set -a; . ~/ib-gateway/.env; set +a
#:
#: is enough to supply it -- the same shell line that starts the container.
#: Nothing here reads that project's files; the variable is the whole contract.
VNC_PASSWORD_ENV = "VNC_SERVER_PASSWORD"


def screen_url(base: str, password: str | None) -> str:
    """`base` with this layer's parameters, and the password when there is one.

    Any parameter already present on `base` wins, so a caller who supplied a
    whole URL keeps their own `resize=`. A `password=` already on it is
    dropped: the resolved one is the answer, and two of them is not a URL
    anybody meant to write.
    """
    parts = urlsplit(base)
    params = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != PASSWORD_PARAM
    ]
    present = {key for key, _ in params}
    params += [(key, value) for key, value in SCREEN_PARAMS if key not in present]
    if password:
        params.append((PASSWORD_PARAM, password))
    return urlunsplit(parts._replace(query=urlencode(params)))


class IBKRGateway(Gateway):
    name = "ibkr"

    spec = SourceSpec(
        name="ibkr",
        fields=(
            Field("host", required=False, default="127.0.0.1", doc="the gateway host"),
            Field("screen_port", required=False, default=6080, cast=int, doc="noVNC; 0 = none"),
            Field("screen_url", required=False, doc="whole noVNC URL, overriding host/port"),
            Field("vnc_port", required=False, default=5900, cast=int, doc="native VNC; 0 = none"),
            Field(
                "control_port",
                required=False,
                default=7462,
                cast=int,
                doc="IBC command server; 0 = none",
            ),
            Field(
                "vnc_password",
                required=False,
                secret=True,
                aliases=("vnc_server_password",),
                doc=f"noVNC prefill; also read from ${VNC_PASSWORD_ENV}",
            ),
            Field("control_timeout", required=False, default=ibc.TIMEOUT, cast=float),
        ),
        profiles=True,
        default_profile="paper",
        doc="IB Gateway's screen and IBC's command server.",
    )

    def host(self, cred: Credential) -> str:
        return str(cred.get("host") or "127.0.0.1")

    def control_port(self, cred: Credential) -> int:
        return int(cred.get("control_port") or 0)

    def vnc_password(self, cred: Credential) -> str | None:
        """The VNC password, from antlia's config or the gateway's own variable.

        This is not a broker credential -- it protects a view of a screen, and
        the broker's own password is typed by IBC inside the container where
        antlia cannot see it. That is why this layer may hold one at all.
        """
        configured = cred.get("vnc_password")
        if configured:
            return str(configured)
        return os.environ.get(VNC_PASSWORD_ENV) or None

    def screen(self, cred: Credential) -> str | None:
        override = cred.get("screen_url")
        if override:
            return str(override)
        port = int(cred.get("screen_port") or 0)
        if not port:
            return None
        return f"http://{self.host(cred)}:{port}{SCREEN_PATH}"

    def describe(self, profile: str | None = None) -> GatewayInfo:
        cred = self.credential(profile)
        host = self.host(cred)
        base = self.screen(cred)
        password = self.vnc_password(cred) if base else None
        vnc_port = int(cred.get("vnc_port") or 0)
        control_port = self.control_port(cred)

        return GatewayInfo(
            source=self.name,
            profile=cred.profile,
            screen_url=screen_url(base, password) if base else None,
            screen_prefilled=bool(base and password),
            vnc_addr=f"{host}:{vnc_port}" if vnc_port else None,
            # Probed, because `control` is a claim about right now and a stale
            # "yes" sends a consumer to a button that cannot work. The screen
            # is deliberately not probed: the browser is the better detector,
            # and a describe that waits on two sockets is twice as slow for an
            # answer nobody acts on.
            control=bool(control_port) and ibc.reachable(host, control_port),
            control_addr=f"{host}:{control_port}" if control_port else None,
        )

    def command(self, text: str, profile: str | None = None) -> str:
        cred = self.credential(profile)
        port = self.control_port(cred)
        if not port:
            raise ControlUnavailable(
                self.name,
                "no control port is configured; set control_port in [ibkr] "
                "(or $ANTLIA_IBKR_CONTROL_PORT) to the port IBC's command server is "
                "published on",
            )
        return ibc.send(
            self.name,
            self.host(cred),
            port,
            text,
            timeout=float(cred.get("control_timeout") or ibc.TIMEOUT),
        )
