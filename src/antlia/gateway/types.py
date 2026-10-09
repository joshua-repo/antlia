"""What a consumer is told about a gateway.

One dataclass, and one rule about one of its fields.

**`screen_url` may carry a password, and must never be written to disk.** The
whole reason the password is in the URL is that noVNC's own credential dialog
is an ordinary `<input type="password">` in an ordinary web page, so password
managers and keyboard extensions fight the user for it -- inside an iframe as
much as outside, since extensions inject into every frame. Prefilling removes
the field rather than winning the fight.

The cost is that the value is now a secret in a string that looks like
configuration, and strings like that end up in logs, in cached payloads and in
saved HTML. So the redaction is built into the type rather than left to each
caller's discipline: `__repr__` and `redacted()` both go through
`safe_screen_url`, and the only way to obtain the password-bearing form is to
ask for the attribute by name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: The query parameter noVNC 1.4 reads its password from (`ui.js`,
#: `getConfigVar`). With `autoconnect=1` set alongside it, no dialog is drawn.
PASSWORD_PARAM = "password"


def strip_password(url: str) -> str:
    """The same URL with the password parameter removed.

    Everything else is preserved, including parameters this layer did not add,
    so the result is still a working link -- it just asks for the password.
    """
    parts = urlsplit(url)
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != PASSWORD_PARAM
    ]
    return urlunsplit(parts._replace(query=urlencode(kept)))


@dataclass(frozen=True, slots=True)
class GatewayInfo:
    """Where a source's gateway is, and what can be done to it.

    `describe()` returns one of these, or `None` for a source that has no
    gateway at all. Every field is optional because every part of a gateway is
    optional: a deployment may publish the screen and not the control channel,
    or the reverse, and neither is a fault.
    """

    #: The antlia source this gateway belongs to, and the profile it was
    #: described for -- so a consumer holding two of these can tell them apart.
    source: str
    profile: str | None = None
    #: noVNC page, ready to open, **with the password already filled in when
    #: there is one**. Do not persist it; see the module docstring.
    screen_url: str | None = None
    #: Whether `screen_url` carries a password, so a panel can say which of the
    #: two situations the reader is in rather than letting them find out.
    screen_prefilled: bool = False
    #: The VNC server as a native viewer would dial it, e.g. "127.0.0.1:5900".
    #: The escape hatch for the one thing a browser cannot fix: noVNC does not
    #: use the Keyboard Lock API, so the browser and its extensions see
    #: keystrokes before the remote screen does. That is a property of the
    #: browser, not of the web client, so no web client fixes it.
    vnc_addr: str | None = None
    #: Whether the control channel answered just now. False is an ordinary
    #: answer: IBC ships its command server disabled.
    control: bool = False
    #: Where that channel is configured to be, whether or not it answered --
    #: so a consumer can say which port to go and look at.
    control_addr: str | None = None

    @property
    def safe_screen_url(self) -> str | None:
        """`screen_url` with the password removed. Use this for anything kept."""
        return None if self.screen_url is None else strip_password(self.screen_url)

    def redacted(self) -> dict[str, Any]:
        """A plain dict with no secret in it, for printing or serialising."""
        return {
            "source": self.source,
            "profile": self.profile,
            "screen_url": self.safe_screen_url,
            "screen_prefilled": self.screen_prefilled,
            "vnc_addr": self.vnc_addr,
            "control": self.control,
            "control_addr": self.control_addr,
        }

    def __repr__(self) -> str:
        label = self.source if self.profile is None else f"{self.source}:{self.profile}"
        return (
            f"GatewayInfo({label}, screen={self.safe_screen_url!r}"
            f"{' +password' if self.screen_prefilled else ''}, "
            f"vnc={self.vnc_addr!r}, control={self.control_addr!r} "
            f"{'up' if self.control else 'down'})"
        )
