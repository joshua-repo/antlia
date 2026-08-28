"""Trading212 REST.

An API key in a header, which makes this the simplest source here -- and the
one where the rate limit matters most. Trading212 limits *per endpoint*, some as
tight as one call every few seconds, and answers a breach with 429s rather than
a queue. The default below is deliberately conservative; raise it per plan with
`rate_limit`, and pace the tight endpoints yourself via `auth.limiter()`.

Demo and live are different hosts, so they are profiles, and the key from one is
not valid on the other.
"""

from __future__ import annotations

import contextlib
from typing import Any

from antlia.auth.base import Provider
from antlia.auth.credentials import Credential
from antlia.auth.errors import ConnectionFailed
from antlia.auth.ratelimit import Limiter
from antlia.auth.spec import Field, SourceSpec

PROFILE_HOSTS = {
    "live": "https://live.trading212.com",
    "demo": "https://demo.trading212.com",
}


class Trading212Provider(Provider):
    spec = SourceSpec(
        name="trading212",
        fields=(
            Field("api_key", secret=True, doc="from Settings -> API (Beta)"),
            Field("base_url", required=False, doc="defaults to the profile's host"),
            Field("timeout", required=False, default=30.0, cast=float),
            Field("rate_limit", required=False, cast=float, doc="calls/sec override"),
        ),
        extra="trading212",
        package="httpx",
        profiles=True,
        default_profile="live",
        identity=("api_key", "base_url"),
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

    def describe(self, cred: Credential) -> dict[str, Any]:
        described = cred.redacted()
        with contextlib.suppress(ConnectionFailed):
            described["base_url"] = self.base_url(cred)
        return described

    def connect(self, cred: Credential, limiter: Limiter) -> Any:
        httpx = self.require("httpx")
        return httpx.Client(
            base_url=self.base_url(cred),
            headers={"Authorization": str(cred["api_key"])},
            timeout=float(cred.get("timeout", 30.0)),
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
