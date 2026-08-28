"""ThetaData, via the vendor SDK.

Written against `thetadata` 1.0.x, which is the **cloud gRPC client**, not the
older local-Terminal one: `ThetaClient(...)` POSTs to ThetaData's auth service
during construction and then opens a gRPC channel to `mdds-01.thetadata.us`.

Two consequences worth knowing before changing anything here:

- **Constructing the client already authenticates.** Unlike the HTTP sources, a
  bad credential fails at `connect()` rather than surviving to the first real
  call. `verify()` still earns its place, because auth succeeding does not prove
  the data channel does.
- **Credentials are `api_key`, or `email` + `password`, or a `creds_file`** --
  one of three, which a per-field `required` flag cannot express. The check is in
  `connect()`, and it names all three options.

Constructor keywords are mapped explicitly and then filtered against the
installed signature, so an SDK that renames or drops one degrades to "that
setting was ignored" instead of a TypeError that reads like a credential
problem. The SDK also imports `python-dotenv` without declaring it, which is why
the extra carries it.
"""

from __future__ import annotations

import inspect
from typing import Any

from antlia.auth.base import Provider
from antlia.auth.credentials import Credential
from antlia.auth.errors import ConnectionFailed, MissingCredential
from antlia.auth.ratelimit import Limiter
from antlia.auth.spec import Field, SourceSpec

#: Credential field -> constructor keyword, current spelling first.
_KEYWORDS = {
    "api_key": ("api_key",),
    "email": ("email", "username"),
    "password": ("password", "passwd"),
    "creds_file": ("creds_file",),
    "mdds_host": ("mdds_host", "host"),
    "mdds_port": ("mdds_port", "port"),
    "dataframe_type": ("dataframe_type",),
}

#: Cheap enough to run on every verify, and it exercises the gRPC data path
#: rather than only the auth POST that construction already did.
VERIFY_SYMBOL = "AAPL"


class ThetaDataProvider(Provider):
    spec = SourceSpec(
        name="thetadata",
        fields=(
            Field("api_key", required=False, secret=True, doc="preferred; beats email+password"),
            Field("email", required=False, secret=True),
            Field("password", required=False, secret=True),
            Field("creds_file", required=False, doc="path to a vendor credentials file"),
            Field("mdds_host", required=False, doc="override the data host"),
            Field("mdds_port", required=False, doc="override the data port"),
            Field("dataframe_type", required=False, doc="'polars' (SDK default) or 'pandas'"),
            Field("rate_limit", required=False, cast=float, doc="calls/sec override"),
        ),
        extra="thetadata",
        package="thetadata",
        identity=("api_key", "email", "mdds_host"),
        # Tier-dependent, so no default: set rate_limit for your plan rather
        # than discovering the ceiling as intermittent failures.
        rate=None,
        doc="ThetaData historical options and equities (cloud gRPC).",
    )

    def _kwargs(self, ctor: Any, cred: Credential) -> dict[str, Any]:
        try:
            accepted = set(inspect.signature(ctor).parameters)
        except (TypeError, ValueError):
            accepted = set()
        kwargs: dict[str, Any] = {}
        for name, spellings in _KEYWORDS.items():
            value = cred.get(name)
            if value is None:
                continue
            for spelling in spellings:
                if spelling in accepted:
                    kwargs[spelling] = value
                    break
        return kwargs

    def _check_one_of(self, cred: Credential) -> None:
        has_pair = bool(cred.get("email")) and bool(cred.get("password"))
        if cred.get("api_key") or cred.get("creds_file") or has_pair:
            return
        raise MissingCredential(
            "thetadata",
            "api_key (or email + password, or creds_file)",
            [
                "$ANTLIA_THETADATA_API_KEY",
                "$ANTLIA_THETADATA_EMAIL together with $ANTLIA_THETADATA_PASSWORD",
                "[thetadata] api_key, or email + password, or creds_file",
            ],
        )

    def connect(self, cred: Credential, limiter: Limiter) -> Any:
        self._check_one_of(cred)
        thetadata = self.require("thetadata")
        ctor = getattr(thetadata, "ThetaClient", None)
        if ctor is None:
            raise ConnectionFailed(
                "thetadata",
                "the installed 'thetadata' package has no ThetaClient; "
                "check the version against the vendor docs",
            )
        try:
            # This call authenticates -- a bad key raises here, not later.
            return ctor(**self._kwargs(ctor, cred))
        except Exception as exc:
            raise ConnectionFailed("thetadata", f"{type(exc).__name__}: {exc}") from exc

    def verify(self, handle: Any) -> str:
        host = getattr(handle, "mdds_host", "?")
        if not getattr(handle, "auth_token", None):
            raise ConnectionFailed(
                "thetadata", "the client holds no auth token -- construction did not authenticate"
            )
        try:
            expirations = handle.option_list_expirations(VERIFY_SYMBOL)
        except Exception as exc:
            raise ConnectionFailed(
                "thetadata",
                f"authenticated, but the data channel failed: {type(exc).__name__}: {exc} "
                f"(host {host}:{getattr(handle, 'mdds_port', '?')})",
            ) from exc
        count = len(expirations) if expirations is not None else 0
        return f"{VERIFY_SYMBOL}: {count} expirations via {host}"

    def disconnect(self, handle: Any) -> None:
        # 1.0.x keeps no closable handle: the gRPC channel is a local in the
        # constructor, captured by the stub and collected with it.
        for name in ("close", "disconnect", "kill"):
            closer = getattr(handle, name, None)
            if callable(closer):
                closer()
                return
