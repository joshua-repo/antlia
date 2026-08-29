"""What a source needs in order to be reachable.

A `SourceSpec` is data, not code: the fields a source requires, what they are
called in the environment and in the config file, what the client library is,
and how hard the source may be hit. Adding a source starts here, and a spec that
is complete enough is most of the adapter.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any


def _to_bool(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"expected a boolean, got {raw!r}")


@dataclass(frozen=True, slots=True)
class Field:
    """One resolvable setting for a source.

    `secret=True` only controls redaction in reprs and logs. It is not
    encryption, and this module does not pretend to offer any.
    """

    name: str
    required: bool = True
    default: Any = None
    secret: bool = False
    # Any, not object: the builtins used here (int, float, bool, str) are
    # declared to take specific types, and a stricter annotation only forces
    # every spec to wrap them in a lambda.
    cast: Callable[[Any], Any] = str
    doc: str = ""
    #: Other spellings this field answers to -- what the vendor's own UI calls
    #: it. Someone copying "Secret key" off a settings page should not have to
    #: know antlia named the field `api_secret`.
    aliases: tuple[str, ...] = ()

    def spellings(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    def parse(self, raw: object) -> Any:
        if self.cast is bool:
            return _to_bool(raw)
        return self.cast(raw)


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """The declaration of a source.

    `extra`/`package` name the optional dependency, so a missing SDK produces a
    `MissingExtra` naming the exact `pip install` rather than an ImportError
    from three frames deep inside an adapter.

    `rate` is the sustained call budget in calls/second and `burst` the bucket
    depth. They belong here, with the credential, because rate limiting is a
    property of the account you authenticated as -- not of any one caller.
    """

    name: str
    fields: Sequence[Field] = ()
    extra: str | None = None
    package: str | None = None
    profiles: bool = False
    default_profile: str | None = None
    rate: float | None = None
    burst: int = 1
    #: Whether the vendor's client may be used from several threads at once.
    #: False serialises access to a pooled session, because the alternative for
    #: a single-threaded SDK is not a race but a deadlock.
    thread_safe: bool = True
    doc: str = ""
    #: Fields whose value identifies the session; two requests agreeing on
    #: these share one connection. Empty means "all resolved fields".
    identity: Sequence[str] = field(default_factory=tuple)

    def field_map(self) -> dict[str, Field]:
        return {f.name: f for f in self.fields}
