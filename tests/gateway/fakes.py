"""A stand-in for IBC's command server.

IBC is a Java UI robot in a container; what it exposes is a socket that takes a
line and answers with one. That is small enough to reproduce exactly, so these
tests exercise the real `socket` code path rather than a mock of it -- which
matters here, because every failure this layer reports is a socket failure and
a mock would be asserting the shape of the mock.
"""

from __future__ import annotations

import socket
import threading


class FakeIBC:
    """Accepts connections on an ephemeral port and answers each with `reply`.

    `reply=None` accepts and says nothing, which is the wedged gateway: IBC is
    alive but waiting on a modal dialog, and the caller must time out.
    """

    def __init__(self, reply: str | None = "OK Restarting"):
        self.reply = reply
        self.commands: list[str] = []
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.host, self.port = self._sock.getsockname()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(1.0)
                try:
                    data = conn.recv(4096)
                except OSError:
                    continue
                if data:
                    self.commands.append(data.decode().strip())
                if self.reply is not None:
                    conn.sendall(self.reply.encode() + b"\n")
                elif data:
                    # Hold the connection open with nothing to say, so the
                    # caller's recv blocks and its timeout is what ends this.
                    self._stop.wait(2.0)

    def close(self) -> None:
        self._stop.set()
        self._sock.close()

    def __enter__(self) -> FakeIBC:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def dead_port() -> int:
    """A port nothing is listening on -- bound, read, and released."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
