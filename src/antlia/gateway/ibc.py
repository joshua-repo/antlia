"""IBC's command server: a line of text in, a line of text out.

IBC is the robot that drives IB Gateway's Swing UI. The gateway has **no
headless mode and no login API**, so IBC types the credentials into the dialog
itself, from inside the container. Its command server is a plain-text TCP
channel into that robot, and it is the only way to ask the gateway to do
anything without a human at a screen.

Three facts that decide how this module behaves:

1. **`RESTART` is a soft restart, not a fresh login.** IBC implements it by
   setting the gateway's own auto-restart time a minute ahead (its log says
   `Setting auto-restart time to 11:42 AM`), which is IBKR's **session
   preserving** restart: it does **not** re-authenticate and it does **not**
   push a new two-factor notification. Documentation claiming otherwise was
   written once and disproved by the gateway's own log. What it does need is a
   UI that can respond -- with the gateway stuck behind a modal dialog, IBC
   sits on `Waiting for config dialog future to complete` for an hour.
2. **The command server is off by default.** IBC's `CommandServerPort` is `0`
   unless a vendored template says otherwise, so "nothing is listening" is the
   *normal* state of an unmodified gateway. `reachable()` exists so that answer
   can be reported as a fact rather than raised as a failure.
3. **The reply is prose, and nothing here parses it.** IBC answers `OK ...` or
   `ERROR ...`, sentences meant for a person. The branch a caller takes is "did
   the socket work"; the words are passed through verbatim for someone to read.
"""

from __future__ import annotations

import socket

from antlia.gateway.errors import ControlUnavailable

#: Generous on purpose: the command is answered by a Java UI robot that may be
#: mid-dialog, and this is a button somebody just pressed.
TIMEOUT = 15.0

#: `describe()` calls `reachable()` on every invocation, so this is short --
#: it is a liveness question on a socket that is either local or on the same
#: LAN, and a describe that blocks is worse than one that says "down".
PROBE_TIMEOUT = 0.5


def reachable(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    """Whether something accepts a connection there, right now.

    Connect and close: it sends no command, so it cannot disturb a gateway
    mid-login. A refusal is the expected answer on a stock gateway and is not
    an error -- it is the value.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def send(source: str, host: str, port: int, command: str, timeout: float = TIMEOUT) -> str:
    """Send one command and return IBC's reply verbatim.

    Raises `ControlUnavailable` when the channel cannot be reached. The two
    failures are distinguished in the message because they call for different
    actions: a refusal means the command server was never enabled, a timeout
    means it was and something is wedged.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(command.encode() + b"\n")
            reply = sock.recv(4096).decode(errors="replace").strip()
    except ConnectionRefusedError as exc:
        raise ControlUnavailable(
            source,
            f"nothing is listening on {host}:{port}. IBC's command server ships disabled "
            f"(CommandServerPort=0) and is switched on by applying "
            f"ops/ib-gateway/ib-gateway.override.yml to the gateway container",
        ) from exc
    except TimeoutError as exc:
        raise ControlUnavailable(
            source,
            f"{host}:{port} accepted the connection but did not answer within {timeout:g}s. "
            f"IBC waits on the gateway's UI, so this is what a gateway stuck behind a modal "
            f"dialog looks like -- look at its screen",
        ) from exc
    except OSError as exc:
        raise ControlUnavailable(source, f"{host}:{port}: {type(exc).__name__}: {exc}") from exc
    return reply or "(no reply)"
