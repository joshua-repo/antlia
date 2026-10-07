"""Telling a gateway to do something.

One command reaches the outside world from here, and it is not an execution
path: `RESTART` restarts a login robot. Antlia still places no orders, and the
socket `auth` opens to the gateway is still `readonly=True`.

Errors are prose for a person. `ControlUnavailable` (a `ConnectionFailed`) says
which of "it was never switched on" and "it is wedged" happened, because those
call for different actions -- but a consumer's branch is whether it raised, not
what it said. pictor prints the sentence above the gateway's own screen, where
the reader can compare it with what the gateway is actually showing.
"""

from __future__ import annotations

from antlia.gateway import registry
from antlia.gateway.base import RESTART
from antlia.gateway.errors import ControlUnavailable


def command(source: str, text: str, profile: str | None = None) -> str:
    """Send one command to `source`'s gateway and return the reply verbatim.

    The general form. `restart()` is the one worth reaching for; the others
    IBC accepts (`STOP`, `RECONNECTDATA`, `ENABLEAPI`) are deliberately not
    given named wrappers -- a button that shuts the gateway down is not a
    recovery action, and wrapping it would make it look like one.
    """
    gateway = registry.get(source)
    if gateway is None:
        raise ControlUnavailable(
            source, f"{source} has no gateway -- there is nothing to send a command to"
        )
    return gateway.command(text, profile)


def restart(source: str, profile: str | None = None) -> str:
    """Ask the gateway to restart itself. Returns what it said.

    **This is a soft restart and not a fresh login.** IBC implements it by
    setting the gateway's own auto-restart a minute ahead, which is IBKR's
    session-preserving restart: no re-authentication, and **no new two-factor
    push**. If what you need is a 2FA prompt, this is not the call -- stop the
    container and start it again, and answer the prompt on the screen.

    It also needs a gateway whose UI can respond. Behind a modal dialog, IBC
    waits indefinitely and this call times out; the screen is what says so.
    """
    return command(source, RESTART, profile)
