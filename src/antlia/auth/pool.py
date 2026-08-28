"""The connection pool: resolve, connect, reuse, close.

Connections are **pooled by identity and outlive the `with` block.** Releasing
the last handle does not disconnect -- the entry stays warm until `close()`,
`close_all()`, or interpreter exit. This is deliberate: the sources here are
long-lived sessions to a local daemon (IBKR's gateway, ThetaData's terminal),
where reconnecting in a loop is both slow and, in IBKR's case, a way to burn
through client ids. The refcount exists to stop a `close()` from cutting a
connection out from under a live user, not to decide when to disconnect.

Reuse is keyed by what the provider says makes a session distinct, so two
profiles, or two accounts on one broker, never collapse into one connection.
"""

from __future__ import annotations

import atexit
import contextlib
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from antlia.auth import registry
from antlia.auth.credentials import Credential, resolve
from antlia.auth.errors import AuthError, ConnectionFailed
from antlia.auth.ratelimit import Limiter, limiter_for


@dataclass
class _Entry:
    handle: Any
    provider: Any
    limiter: Limiter
    refcount: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


_pool: dict[tuple[Any, ...], _Entry] = {}
_pool_lock = threading.RLock()


def credential(source: str, profile: str | None = None, **overrides: Any) -> Credential:
    """Resolve a source's settings without opening anything.

    Nothing here touches the network, which is what lets a cache-covered
    backtest run start to finish without ever authenticating.
    """
    provider = registry.get(source)
    return resolve(provider.spec, profile, overrides)


def _key(source: str, profile: str | None, ident: tuple[Any, ...]) -> tuple[Any, ...]:
    return (source, profile, ident)


def _open(
    source: str, profile: str | None, overrides: Mapping[str, Any]
) -> tuple[tuple[Any, ...], _Entry]:
    provider = registry.get(source)
    cred = resolve(provider.spec, profile, overrides)
    key = _key(source, cred.profile, provider.identity(cred))

    with _pool_lock:
        entry = _pool.get(key)
        if entry is not None and provider.healthy(entry.handle):
            return key, entry
        if entry is not None:
            # Stale handle: drop it and reconnect, but never while it is in use.
            if entry.refcount == 0:
                _safe_disconnect(provider, entry.handle)
                del _pool[key]
            else:
                return key, entry

        # A per-source `rate_limit` in the config beats the spec's default:
        # vendor ceilings are a property of the user's plan, not of the code.
        rate = cred.get("rate_limit") or provider.spec.rate
        limiter = limiter_for(source, cred.profile, rate, provider.spec.burst)
        try:
            handle = provider.connect(cred, limiter)
        except AuthError:
            # Already precise -- a missing extra or a named connection failure
            # must not be reworded into a generic one.
            raise
        except Exception as exc:  # adapter bugs and raw SDK errors alike
            raise ConnectionFailed(source, f"{type(exc).__name__}: {exc}") from exc

        entry = _Entry(handle=handle, provider=provider, limiter=limiter)
        _pool[key] = entry
        return key, entry


@contextmanager
def session(source: str, profile: str | None = None, **overrides: Any) -> Iterator[Any]:
    """Yield a live, authenticated client for `source`.

    The object yielded is the vendor's own client -- `ib_insync.IB`, an
    `httpx.Client`, and so on. Antlia owns when it opens and closes; it does not
    wrap how you call it.

        with session("ibkr", profile="paper") as ib:
            ib.positions()

    Keyword arguments override resolved fields for this call (`port=7497`).
    """
    key, entry = _open(source, profile, overrides)
    with _pool_lock:
        entry.refcount += 1
    try:
        yield entry.handle
    finally:
        with _pool_lock:
            entry.refcount = max(0, entry.refcount - 1)


def limiter(source: str, profile: str | None = None) -> Limiter:
    """The rate limiter for a source, for callers that must pace themselves.

    Needed where the SDK's calls cannot be intercepted (yfinance), and harmless
    elsewhere -- the bucket is shared, so an extra `acquire()` is just honest
    accounting.
    """
    provider = registry.get(source)
    # Asking for the limiter is not asking to authenticate, so an unresolvable
    # credential falls back to the spec's rate instead of raising.
    try:
        cred = resolve(provider.spec, profile, {})
        rate = cred.get("rate_limit") or provider.spec.rate
        profile = cred.profile
    except AuthError:
        rate = provider.spec.rate
    return limiter_for(source, profile, rate, provider.spec.burst)


def _safe_disconnect(provider: Any, handle: Any) -> None:
    # A failed close must never mask the caller's work, and a half-closed
    # handle must not stay in the pool -- the caller drops it either way.
    with contextlib.suppress(Exception):
        provider.disconnect(handle)


def close(source: str, profile: str | None = None) -> int:
    """Close pooled sessions for a source (and profile, if given).

    Entries still in use are left alone. Returns the number actually closed.
    """
    closed = 0
    with _pool_lock:
        for key in list(_pool):
            if key[0] != source or (profile is not None and key[1] != profile):
                continue
            entry = _pool[key]
            if entry.refcount > 0:
                continue
            _safe_disconnect(entry.provider, entry.handle)
            del _pool[key]
            closed += 1
    return closed


def close_all() -> int:
    """Close every pooled session, in use or not. Runs at interpreter exit."""
    with _pool_lock:
        keys = list(_pool)
        for key in keys:
            entry = _pool.pop(key)
            _safe_disconnect(entry.provider, entry.handle)
        return len(keys)


def open_sessions() -> list[tuple[str, str | None, int]]:
    """(source, profile, refcount) for each pooled session. For diagnostics."""
    with _pool_lock:
        return [(k[0], k[1], e.refcount) for k, e in _pool.items()]


atexit.register(close_all)
