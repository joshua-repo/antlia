"""The canonical FX schema.

One shape, whichever source answered. A consumer that has to know whether a
rate came from Yahoo or from the ECB in order to read it has been handed the
vendor problem back, which is the thing this layer exists to absorb.

`RateTable` is frozen and stamped because it
is a **reading, not a handle**. FX moves continuously; a table says what was
true at `as_of`, according to `source`, and says so out loud when it is `stale`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

#: Everything crosses through this. Both sources answer USD-based natively --
#: Yahoo's `XXX=X` shorthand is units per 1 USD, Frankfurter takes `base` -- so
#: the pivot costs no extra request and adding a currency stays a one-liner.
PIVOT = "USD"

#: A reasonable starting set. Not a limit: `rates()` takes any currencies both
#: sources carry, and this is only what a caller gets for asking for nothing.
DEFAULT_CURRENCIES: tuple[str, ...] = ("USD", "GBP", "JPY", "HKD")


@dataclass(frozen=True, slots=True)
class RateTable:
    """Units of each currency per 1 unit of `base`, at one instant.

    `USD: 1.0, GBP: 0.7387, JPY: 160.04` reads as "one dollar buys 0.7387
    pounds or 160.04 yen".

    Every rate in a table comes from **one** source. Sources are tried in turn
    and the first complete answer wins; results are never stitched together
    across providers, because a total assembled from two vendors' rates cannot
    be reconciled against either of them.
    """

    base: str
    rates: Mapping[str, float]
    as_of: datetime
    source: str
    #: True when every live source failed and this is the last table on disk.
    #: The consumer decides whether stale is good enough -- but it is told.
    stale: bool = False

    def has(self, currency: str) -> bool:
        return currency in self.rates

    def rate(self, currency: str) -> float | None:
        """Units of `currency` per 1 unit of `base`."""
        return self.rates.get(currency)

    def inverse(self, currency: str) -> float | None:
        """Units of `base` per 1 unit of `currency`.

        The other orientation, and worth having by name because it is the one
        brokers report. IBKR's own exchange rate is base-per-unit -- a
        USD-based account holding yen carries `0.006247` for JPY, not `160.08`
        -- so this is the number to compare it against. Comparing across
        orientations silently produces a rate that is wrong by a factor of
        25,000, and looks plausible in neither direction.
        """
        rate = self.rates.get(currency)
        if not rate:
            return None
        return 1.0 / rate

    def convert(self, amount: float | None, frm: str, to: str) -> float | None:
        """`amount` restated from one currency into another, via the pivot.

        `None` whenever it cannot be done exactly -- an unknown currency, or a
        missing amount. **Never an approximation**: a wrong FX rate mis-states
        a whole portfolio quietly, where an absent one just removes a feature.
        """
        if amount is None or frm == to:
            return amount
        source_rate, target_rate = self.rates.get(frm), self.rates.get(to)
        if not source_rate or target_rate is None:
            return None
        return amount / source_rate * target_rate

    @property
    def age(self) -> timedelta:
        """How long ago `as_of` was. Naive stamps are read as UTC."""
        stamp = self.as_of if self.as_of.tzinfo else self.as_of.replace(tzinfo=UTC)
        return datetime.now(UTC) - stamp

    def __str__(self) -> str:
        quoted = "  ".join(f"{c} {r:.6g}" for c, r in sorted(self.rates.items()))
        mark = " (stale)" if self.stale else ""
        return f"1 {self.base} = {quoted}   [{self.source} {self.as_of:%Y-%m-%d %H:%M}{mark}]"
