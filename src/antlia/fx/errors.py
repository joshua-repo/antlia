"""One error, so the message reads like what actually went wrong.

`ConnectionFailed` renders as *"could not open a 'yfinance' session"*, which is
true of `auth` and false here: the session opened fine, the vendor just had no
rate to give. An error that misdescribes the failure costs the reader a round
of guessing, which is the thing this codebase's errors exist to prevent.

It **subclasses `ConnectionFailed`** rather than replacing it, so the contract
the consumers were promised is unchanged: `except ConnectionFailed` -- or
`except AuthError` -- still catches everything this layer raises.
"""

from __future__ import annotations

from antlia.auth.errors import ConnectionFailed


class RatesUnavailable(ConnectionFailed):
    """A source, or the whole chain, could not price what was asked for."""

    def __init__(self, source: str, detail: str) -> None:
        # Skips ConnectionFailed's "could not open a session" wording, and
        # keeps its type and its `.source` attribute.
        Exception.__init__(self, f"{source}: could not supply FX rates -- {detail}")
        self.source = source
