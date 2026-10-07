"""One error, so the message reads like what actually went wrong.

`ConnectionFailed` renders as *"could not open an 'ibkr' session"*, which is
what `auth` means and not what this layer means: there is no session here, and
the API socket it names may be perfectly healthy while the control channel is
simply switched off. An error that misdescribes the failure costs the reader a
round of guessing, which is the thing this codebase's errors exist to prevent.

It **subclasses `ConnectionFailed`** for the same reason `fx.RatesUnavailable`
does: the contract consumers were promised is unchanged, so `except
ConnectionFailed` -- or `except AuthError` -- still catches everything raised
here, and nobody has to learn a new import to keep working.

The message is prose for a person. A consumer decides on *whether* this was
raised, never on the words in it: pictor prints them above the gateway's own
screen and reads nothing out of them.
"""

from __future__ import annotations

from antlia.auth.errors import ConnectionFailed


class ControlUnavailable(ConnectionFailed):
    """The gateway's control channel could not be reached, or does not exist."""

    def __init__(self, source: str, detail: str) -> None:
        # Skips ConnectionFailed's "could not open a session" wording, and
        # keeps its type and its `.source` attribute.
        Exception.__init__(self, f"{source}: cannot command the gateway -- {detail}")
        self.source = source
