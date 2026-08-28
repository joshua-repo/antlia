"""Every failure this module can produce, and nothing else.

Errors carry what was *tried*, not just what failed. A missing credential that
does not name the environment variable and the file path it looked in costs the
user a round of guessing, and this module exists to keep that guessing out of
the consuming projects.
"""

from __future__ import annotations

from collections.abc import Iterable


class AuthError(Exception):
    """Base class for every error raised by `antlia.auth`."""


class UnknownSource(AuthError):
    def __init__(self, source: str, known: Iterable[str]) -> None:
        known_list = ", ".join(sorted(known))
        super().__init__(f"unknown source {source!r}; registered sources: {known_list}")
        self.source = source


class UnknownProfile(AuthError):
    def __init__(self, source: str, profile: str, known: Iterable[str]) -> None:
        known_list = ", ".join(sorted(known)) or "(none configured)"
        super().__init__(
            f"source {source!r} has no profile {profile!r}; configured profiles: {known_list}"
        )
        self.source = source
        self.profile = profile


class MissingCredential(AuthError):
    """A required field could not be resolved from any layer.

    The message lists every location that was consulted, in order.
    """

    def __init__(self, source: str, field: str, tried: Iterable[str]) -> None:
        locations = "\n  ".join(tried)
        super().__init__(f"no value for {source}.{field}; looked in, in order:\n  {locations}")
        self.source = source
        self.field = field


class InvalidCredential(AuthError):
    """A value was found but could not be used (wrong type, unparseable)."""

    def __init__(self, source: str, field: str, value: object, reason: str) -> None:
        super().__init__(f"{source}.{field}={value!r} is unusable: {reason}")
        self.source = source
        self.field = field


class MissingExtra(AuthError):
    """The source's client library is not installed.

    Adapters are optional extras precisely so that one vendor SDK is never
    everyone's install cost; this is the error that says which one to add.
    """

    def __init__(self, source: str, extra: str, package: str) -> None:
        super().__init__(
            f"source {source!r} needs the {package!r} package: pip install 'antlia[{extra}]'"
        )
        self.source = source
        self.extra = extra
        self.package = package


class ConnectionFailed(AuthError):
    """The credentials resolved but the session could not be established."""

    def __init__(self, source: str, detail: str) -> None:
        super().__init__(f"could not open a {source!r} session: {detail}")
        self.source = source
