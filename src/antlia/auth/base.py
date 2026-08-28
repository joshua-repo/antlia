"""The provider contract.

A provider knows three things and nothing more: what its source needs
(`spec`), how to turn a resolved credential into a live client (`connect`), and
how to put that client away (`disconnect`). Everything above -- resolution
order, rate limiting, connection reuse, lifecycle -- is handled once, here in
`auth`, so no adapter and no consumer reimplements it.

Providers deliberately return the **vendor's own client object**, not a wrapper.
Antlia does not have an opinion about how you call IBKR; it has an opinion about
who owns the connection. Wrapping the client would smuggle a data-access API
into the authentication layer, and that boundary is the point of the module.
"""

from __future__ import annotations

import abc
import importlib
from types import ModuleType
from typing import Any

from antlia.auth.credentials import Credential
from antlia.auth.errors import MissingExtra
from antlia.auth.ratelimit import Limiter
from antlia.auth.spec import SourceSpec


class Provider(abc.ABC):
    """Base class for source adapters."""

    spec: SourceSpec

    @abc.abstractmethod
    def connect(self, cred: Credential, limiter: Limiter) -> Any:
        """Open a live client. Raise `ConnectionFailed` on failure."""

    def disconnect(self, handle: Any) -> None:  # noqa: B027 -- optional by design
        """Close a client opened by `connect`. Must tolerate a dead handle.

        Not abstract: a source with nothing to close (yfinance) should not be
        made to write an empty override to prove it.
        """

    def healthy(self, handle: Any) -> bool:
        """Whether a pooled handle is still usable. False triggers a reconnect."""
        return True

    def identity(self, cred: Credential) -> tuple[Any, ...]:
        """What makes two requests the same session.

        Defaults to the spec's `identity` fields, or every resolved value.
        Secrets are included -- two different keys are two different sessions --
        and the tuple is never logged.
        """
        names = self.spec.identity or sorted(cred.values)
        return tuple(cred.get(n) for n in names)

    def verify(self, handle: Any) -> str:
        """Prove the session actually works, with the cheapest real round-trip.

        Returns a short human-readable result, or raises `ConnectionFailed`.

        This exists because `connect()` succeeding proves very little for some
        sources -- an HTTP client with a wrong API key constructs perfectly, and
        fails hours later inside a backtest. A verify is what turns "the object
        was created" into "the credential is good".
        """
        return "no verification defined for this source"

    def describe(self, cred: Credential) -> dict[str, Any]:
        """Redacted settings for diagnostics.

        Override where a value is *derived* rather than resolved -- a doctor
        that prints `port: None` for a connection that will use 7497 is
        reporting the config, not the truth.
        """
        return cred.redacted()

    def require(self, module: str) -> ModuleType:
        """Import the vendor SDK, or say precisely which extra is missing."""
        try:
            return importlib.import_module(module)
        except ImportError as exc:
            raise MissingExtra(
                self.spec.name,
                self.spec.extra or self.spec.name,
                self.spec.package or module,
            ) from exc
