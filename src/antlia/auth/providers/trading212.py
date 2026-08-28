"""Trading212 REST.

**Credentials are a key/secret pair sent as HTTP Basic**, not a bare token:
`Authorization: Basic base64("<key>:<secret>")`. Trading212 issues the two
together in Settings -> API, and a key without its secret authenticates nothing.
Confirmed against a working integration (`joshua-repo/t212-mcp`'s `server.py`).

A bare key in the `Authorization` header is still accepted where an older
single-token credential exists, so `api_secret` is optional -- but which scheme
is in use is reported by the doctor rather than chosen silently, because "401
and no idea why" is the failure this would otherwise produce.

This is also the source where the rate limit matters most. Trading212 limits *per endpoint*, some as
tight as one call every few seconds, and answers a breach with 429s rather than
a queue. The default below is deliberately conservative; raise it per plan with
`rate_limit`.

**Rate limiting is per endpoint, and self-calibrating.** T212 budgets each path
separately -- one call per 30s to `account/info`, one per 2s to `account/cash`,
six a minute to `history/orders` -- and reports the budget in `x-ratelimit-*`
response headers. A request hook pays a token into that endpoint's own bucket; a
response hook replaces the bucket with whatever the vendor just said the limit
is. So the first call guesses, and every call after that is paced by fact.

One shared bucket cannot express this: set it to the tightest limit and
everything crawls, set it to the loosest and `account/info` 429s. Both hooks are
httpx's own mechanism, so the object returned is still a plain `httpx.Client`
and not a wrapper.

Demo and live are different hosts, so they are profiles, and the key from one is
not valid on the other.
"""

from __future__ import annotations

import base64
import contextlib
from typing import Any

from antlia.auth.base import Provider
from antlia.auth.credentials import Credential
from antlia.auth.errors import ConnectionFailed
from antlia.auth.ratelimit import Limiter, endpoint_limiter, observe_limit
from antlia.auth.spec import Field, SourceSpec


def to_number(raw: object) -> float | None:
    """Header values are strings, and absent ones are None."""
    try:
        return float(str(raw))
    except (TypeError, ValueError):
        return None


PROFILE_HOSTS = {
    "live": "https://live.trading212.com",
    "demo": "https://demo.trading212.com",
}


class Trading212Provider(Provider):
    spec = SourceSpec(
        name="trading212",
        fields=(
            Field(
                "api_key",
                secret=True,
                doc="from Settings -> API; pairs with api_secret",
                aliases=("key",),
            ),
            Field(
                "api_secret",
                required=False,
                secret=True,
                doc="the other half of the pair; Basic auth is used when present",
                # What T212's settings page calls it.
                aliases=("secret_key", "secret"),
            ),
            Field("base_url", required=False, doc="defaults to the profile's host"),
            Field("timeout", required=False, default=30.0, cast=float),
            Field("rate_limit", required=False, cast=float, doc="calls/sec override"),
        ),
        extra="trading212",
        package="httpx",
        profiles=True,
        default_profile="live",
        identity=("api_key", "api_secret", "base_url"),
        rate=1.0,
        burst=1,
        doc="Trading212 REST API.",
    )

    def base_url(self, cred: Credential) -> str:
        explicit = cred.get("base_url")
        if explicit:
            return str(explicit).rstrip("/")
        host = PROFILE_HOSTS.get(cred.profile or "live")
        if host is None:
            raise ConnectionFailed(
                "trading212",
                f"no host for profile {cred.profile!r}; set base_url or use "
                f"profile {' / '.join(sorted(PROFILE_HOSTS))}",
            )
        return host

    def auth_header(self, cred: Credential) -> str:
        """`Basic base64(key:secret)` when a secret exists, else the bare key."""
        secret = cred.get("api_secret")
        if secret:
            pair = f"{cred['api_key']}:{secret}".encode()
            return "Basic " + base64.b64encode(pair).decode()
        return str(cred["api_key"])

    def describe(self, cred: Credential) -> dict[str, Any]:
        described = cred.redacted()
        with contextlib.suppress(ConnectionFailed):
            described["base_url"] = self.base_url(cred)
        # Which scheme is in play decides whether a 401 means "wrong key" or
        # "you forgot the secret". Never leave that to be guessed.
        described["auth_scheme"] = "basic" if cred.get("api_secret") else "bare-key"
        return described

    def connect(self, cred: Credential, limiter: Limiter) -> Any:
        httpx = self.require("httpx")
        profile = cred.profile
        fallback = getattr(limiter, "rate", None)

        def pace(request: Any) -> None:
            endpoint_limiter(self.spec.name, profile, request.url.path, fallback).acquire()

        def learn(response: Any) -> None:
            limit = to_number(response.headers.get("x-ratelimit-limit"))
            period = to_number(response.headers.get("x-ratelimit-period"))
            if limit is not None and period is not None:
                observe_limit(self.spec.name, profile, response.request.url.path, limit, period)

        return httpx.Client(
            base_url=self.base_url(cred),
            headers={"Authorization": self.auth_header(cred)},
            timeout=float(cred.get("timeout", 30.0)),
            event_hooks={"request": [pace], "response": [learn]},
        )

    def verify(self, handle: Any) -> str:
        try:
            response = handle.get("/api/v0/equity/account/info")
        except Exception as exc:
            raise ConnectionFailed("trading212", f"{type(exc).__name__}: {exc}") from exc
        if response.status_code == 401:
            raise ConnectionFailed("trading212", "401 -- the API key is not valid for this host")
        if response.status_code == 403:
            raise ConnectionFailed("trading212", "403 -- the key lacks the scope for account/info")
        if response.status_code == 429:
            raise ConnectionFailed(
                "trading212", "429 -- rate limited; lower rate_limit in the config"
            )
        if response.status_code >= 400:
            raise ConnectionFailed("trading212", f"HTTP {response.status_code}")
        payload = response.json()
        return f"account {payload.get('id', '?')} ({payload.get('currencyCode', '?')})"

    def healthy(self, handle: Any) -> bool:
        return not getattr(handle, "is_closed", False)

    def disconnect(self, handle: Any) -> None:
        handle.close()
