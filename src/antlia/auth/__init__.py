"""Credentials, sessions and rate limits for every data source.

Antlia's bottom layer. It answers one question -- *give me a working client for
source X* -- and keeps the answer out of every module above it.

    from antlia import auth

    with auth.session("ibkr", profile="paper") as ib:
        positions = ib.positions()

    with auth.session("trading212") as t212:
        t212.get("/api/v0/equity/portfolio")

What it owns: resolution order (environment, then `~/.antlia/credentials.toml`),
connection lifecycle and reuse, rate limiting per account, and error messages
that name the variable and the file they looked in. What it does not own: how
you call the vendor's client. `session()` yields the vendor's own object.

It imports nothing else from antlia and has no required dependencies, so a
consumer that wants only authentication is not made to install a query engine.
Vendor SDKs are optional extras, imported when their source is first used --
`antlia[ibkr]`, `antlia[thetadata]`, `antlia[trading212]`, `antlia[yfinance]`.

Adding a source: write a `Provider` with a `SourceSpec` and `register()` it.
"""

from __future__ import annotations

from antlia.auth.base import Provider
from antlia.auth.credentials import (
    Credential,
    check_permissions,
    config_path,
    home,
    profiles,
)
from antlia.auth.errors import (
    AuthError,
    ConnectionFailed,
    InvalidCredential,
    MissingCredential,
    MissingExtra,
    UnknownProfile,
    UnknownSource,
)
from antlia.auth.pool import (
    close,
    close_all,
    credential,
    limiter,
    open_sessions,
    session,
    verify,
)
from antlia.auth.ratelimit import (
    Limiter,
    TokenBucket,
    Unlimited,
    endpoint_limiter,
    observe_limit,
)
from antlia.auth.registry import get as provider
from antlia.auth.registry import register, sources, unregister
from antlia.auth.spec import Field, SourceSpec

__all__ = [
    # the two calls most consumers need
    "session",
    "credential",
    # proving a session is usable, not merely open
    "verify",
    # lifecycle
    "close",
    "close_all",
    "open_sessions",
    # rate limiting
    "limiter",
    "Limiter",
    "TokenBucket",
    "Unlimited",
    "endpoint_limiter",
    "observe_limit",
    # registry / extension
    "provider",
    "register",
    "unregister",
    "sources",
    "Provider",
    "SourceSpec",
    "Field",
    # configuration
    "Credential",
    "config_path",
    "home",
    "profiles",
    "check_permissions",
    # errors
    "AuthError",
    "ConnectionFailed",
    "InvalidCredential",
    "MissingCredential",
    "MissingExtra",
    "UnknownProfile",
    "UnknownSource",
]
