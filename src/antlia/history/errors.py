"""Errors that say which of the three failures happened.

There are exactly three ways a historical read goes wrong, and a consumer
handles each of them differently:

- **The store does not hold it** (`NotCovered`). Nothing is broken. Either
  fetch it, or narrow the window. The error names the missing ranges, so a
  caller does not have to go and work them out.
- **The source will never serve it** (`NotEntitled`). A metered plan, a
  window before the vendor's history begins. Retrying is pointless and a
  fetch loop that cannot tell this from a network blip will retry forever.
- **The fetch failed** (`IngestFailed`). Transient, or a bug. Try again.

All three descend from `HistoryError`, and `NotEntitled` additionally from
`auth`'s `ConnectionFailed`, so a consumer already catching `AuthError` around
its data access keeps catching everything the vendor layer raises.
"""

from __future__ import annotations

from antlia.auth.errors import ConnectionFailed
from antlia.history.types import Window


class HistoryError(Exception):
    """Anything the history layer refuses to do."""


class NotCovered(HistoryError):
    """The store does not hold the requested window, and was told not to fetch."""

    def __init__(
        self,
        table: str,
        symbol: str,
        missing: list[Window],
        source: str,
        fix: str | None = None,
    ) -> None:
        gaps = ", ".join(str(w) for w in missing) or "the whole window"
        if fix is None:
            fix = f"antlia-history fill {table} {symbol}"
            if missing:
                # Only offer a runnable command when there are real dates to
                # put in it. A hint with two empty flags is worse than no hint.
                fix += f" --start {missing[0].start} --end {missing[-1].end}"
        super().__init__(
            f"{source}:{table} holds no {symbol} data for {gaps}. "
            f"Pass fetch=True to fill it, or run: {fix}"
        )
        self.table = table
        self.symbol = symbol
        self.missing = missing
        self.source = source


class NotEntitled(ConnectionFailed):
    """The source refused the request outright, and will keep refusing it.

    A plan boundary, not an outage. It subclasses `ConnectionFailed` so the
    existing `except AuthError` in a consumer still catches it, and the ingest
    path treats it as a permanent answer -- recorded in the ledger so the same
    window is never asked for twice.
    """

    def __init__(self, source: str, detail: str, window: Window | None = None) -> None:
        span = f" for {window}" if window else ""
        Exception.__init__(self, f"{source}: not entitled{span} -- {detail}")
        self.source = source
        self.window = window


class IngestFailed(HistoryError):
    """A fetch or a write did not complete. Transient until proven otherwise."""

    def __init__(self, source: str, detail: str) -> None:
        super().__init__(f"{source}: ingest failed -- {detail}")
        self.source = source


# `NotEntitled` is not a `HistoryError` by inheritance -- it is a connection
# fact, and duplicating the base would make `except HistoryError` catch a
# credential problem. Registered here so `except (HistoryError, NotEntitled)`
# is never the thing a reader has to remember.
__all__ = ["HistoryError", "NotCovered", "NotEntitled", "IngestFailed"]
