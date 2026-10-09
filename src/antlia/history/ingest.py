"""The one write path: plan what is missing, fetch it, append it, record it.

Every byte that lands in the store comes through here. That is the rule
CLAUDE.md calls *four read surfaces, exactly one write path*, and it is what
keeps a latency-sensitive read from ever touching a Parquet writer.

The plan is built before a single request goes out, which matters more on a
metered plan than it does anywhere else:

1. **Clamp to the entitlement horizon** at the old end, and to the last
   published session at the new one. ThetaData fails a request whose range
   *straddles* its plan boundary -- it does not trim to what you may read -- so
   a window reaching too far back would cost the whole fetch, not part of it.
   A window reaching past the last published session costs nothing, which is
   worse: the ledger would settle a day the vendor has not summarised yet.
2. **Subtract what the ledger already settles.** Both rows fetched and days
   proven empty. A covered window costs zero requests and never authenticates.
3. **Subtract what the source has already refused.** A permanent refusal
   recorded once is never re-requested; without this the same denial is paid
   for on every run.
4. **Clamp each scope to its own life.** An expiration that expired in March
   is not asked about April. On a three-year option warm-up this is the single
   largest saving available, and it is free.
5. **Split by the vendor's span cap**, exactly, because one day over fails
   the request rather than trimming it.

Progress is written to the ledger as it goes, in batches, so an interrupted
run keeps everything it had already paid for.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from antlia import auth
from antlia.history import ledger, store
from antlia.history.base import HistorySource, Scope
from antlia.history.errors import NotEntitled
from antlia.history.types import TableSpec, Window, subtract

#: Ledger rows buffered before a flush, so an 800-expiration warm-up does not
#: write 800 files.
FLUSH_EVERY = 100

#: ...but a count alone is not enough. One ThetaData request over a 90-day
#: window takes 35 seconds, so a 100-entry buffer is an hour of unrecorded
#: work -- and a run killed by a scheduler's SIGTERM never reaches the `finally`
#: that would have written it. Measured the hard way: twenty minutes of fetched
#: option chains landed in `raw/` with nothing in the ledger, so the retry
#: re-fetched every one of them. Whichever bound comes first wins.
FLUSH_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class Request:
    """One vendor call the plan intends to make."""

    scope: Scope
    window: Window

    def __str__(self) -> str:
        return f"{self.scope.key or '-'} {self.window}"


@dataclass(frozen=True, slots=True)
class Plan:
    """What will be asked for, and what will not."""

    table: str
    symbol: str
    source: str
    requested: Window
    #: `requested` narrowed to what the plan is entitled to read.
    eligible: Window | None
    requests: tuple[Request, ...]
    #: Dropped because the horizon excludes it. Never requested.
    beyond_horizon: Window | None = None
    #: Dropped because the source has not published it yet. Never requested,
    #: and still missing: a later fill picks it up.
    unpublished: Window | None = None

    @property
    def calls(self) -> int:
        return len(self.requests)

    def __str__(self) -> str:
        head = f"{self.symbol} {self.table} via {self.source}: {self.calls} requests"
        if self.beyond_horizon:
            head += f"; {self.beyond_horizon} is before the plan's horizon"
        if self.unpublished:
            head += f"; {self.unpublished} is not yet published"
        if not self.requests and not self.unpublished:
            head += " (already covered)"
        return head


@dataclass(slots=True)
class Report:
    """What one ingest run actually did."""

    table: str
    symbol: str
    source: str
    requested: Window
    calls: int = 0
    rows: int = 0
    empty: int = 0
    denied: int = 0
    seconds: float = 0.0
    entries: list[ledger.Entry] = field(default_factory=list)

    def __str__(self) -> str:
        parts = [
            f"{self.symbol} {self.table} via {self.source} {self.requested}",
            f"{self.calls} requests",
            f"{self.rows} rows",
        ]
        if self.empty:
            parts.append(f"{self.empty} empty")
        if self.denied:
            parts.append(f"{self.denied} denied")
        parts.append(f"{self.seconds:.1f}s")
        return " -- ".join(parts)


def plan(
    base: Path,
    src: HistorySource,
    spec: TableSpec,
    symbol: str,
    window: Window,
    *,
    refresh: bool = False,
    **options: Any,
) -> Plan:
    """What is missing, as the exact requests that would fill it.

    `refresh=True` plans the whole eligible window regardless of what the
    ledger already settles. It is the only way a **second append** for a key
    can ever be produced, and without it the restatement design -- `latest=`,
    `as_of=`, the whole reason `raw/` keeps every version -- is unreachable
    code. Denied windows are re-requested too: an upgraded subscription is
    exactly when you would ask again.
    """
    horizon = src.earliest(spec.name)
    last = src.latest(spec.name)
    eligible = window.clamp(start=horizon, end=last)
    beyond = unpublished = None
    if horizon and window.start < horizon:
        beyond = Window(window.start, min(window.end, horizon - dt.timedelta(days=1)))
    if last and window.end > last:
        unpublished = Window(max(window.start, last + dt.timedelta(days=1)), window.end)

    if eligible is None:
        return Plan(spec.name, symbol, src.name, window, None, (), beyond, unpublished)

    requests: list[Request] = []
    for scope in src.scopes(spec.name, symbol, eligible, **options):
        span = scope.clamp(eligible)
        if span is None:
            continue
        known: list[Window] = []
        if not refresh:
            known = [
                *ledger.windows(base, src.name, spec.name, symbol, scope.key),
                *ledger.denied(base, src.name, spec.name, symbol, scope.key),
            ]
        for gap in subtract(span, known):
            cap = src.max_span_days
            for chunk in gap.chunks(cap) if cap else [gap]:
                requests.append(Request(scope, chunk))

    return Plan(spec.name, symbol, src.name, window, eligible, tuple(requests), beyond, unpublished)


def concurrency(src: HistorySource, workers: int) -> int:
    """`workers`, unless the source's SDK may not be driven from many threads.

    Two limits apply. The vendor's own ceiling comes from the adapter's
        `max_workers` -- exceeding it fails a request rather than queueing it, so
        it is clamped, not negotiated. Whether the SDK may be driven from several
        threads at all comes from `auth`'s `SourceSpec.thread_safe`, because that
        is where the fact lives: an `ib_insync.IB` driven from four threads deadlocks rather
        than races, and the session pool already serialises it. Clamping here lets
        a caller ask for eight without knowing which vendor is behind the adapter,
        and get one where that is the only safe answer.
    """
    if src.max_workers is not None:
        workers = min(workers, src.max_workers)
    if workers <= 1 or src.auth_source is None:
        return max(1, workers)
    try:
        spec = auth.provider(src.auth_source).spec
    except Exception:
        # An unknown auth source is not a reason to refuse to run. It is a
        # reason not to assume it is safe to parallelise.
        return 1
    return workers if spec.thread_safe else 1


def run(
    base: Path,
    src: HistorySource,
    spec: TableSpec,
    symbol: str,
    window: Window,
    *,
    progress: Callable[[Request, ledger.Entry], None] | None = None,
    limit: int | None = None,
    workers: int = 1,
    refresh: bool = False,
    **options: Any,
) -> Report:
    """Execute the plan. Returns what it did, whether or not everything worked.

    `limit` caps the number of vendor requests, which is how a first look at a
    wide window is taken without spending a day's budget on it. A capped run is
    not a failure: what it fetched is recorded, and the next run resumes from
    the ledger exactly where this one stopped.

    `workers` runs that many requests at once. They are independent by
    construction -- one expiration, one date chunk -- and the rate limiter is
    process-wide, so concurrency goes faster without going over the vendor's
    budget. Measured on ThetaData: two threads did in 10.0s what took 20.4s in
    sequence. It defaults to 1 because raising it spends a metered budget
    faster, and that is the caller's decision, not this function's.

    `refresh=True` re-fetches the window even where the ledger settles it,
    appending a new version instead of editing the old one.
    """
    intent = plan(base, src, spec, symbol, window, refresh=refresh, **options)
    report = Report(spec.name, symbol, src.name, window)
    started = time.perf_counter()
    pending: list[ledger.Entry] = []
    date_expression = src.projection(spec.name).get("date")

    requests = intent.requests[:limit] if limit is not None else intent.requests
    flushed = time.monotonic()
    guard = threading.Lock()

    def landed(request: Request, entry: ledger.Entry) -> None:
        """One finished request, accounted for. Serialised across workers."""
        nonlocal pending, flushed
        with guard:
            report.calls += 1
            report.rows += entry.rows
            report.empty += entry.status == ledger.EMPTY
            report.denied += entry.status == ledger.DENIED
            report.entries.append(entry)
            pending.append(entry)
            if progress is not None:
                progress(request, entry)
            stale = time.monotonic() - flushed >= FLUSH_SECONDS
            if len(pending) >= FLUSH_EVERY or stale:
                ledger.record(base, pending)
                pending = []
                flushed = time.monotonic()

    def fetch(request: Request) -> ledger.Entry:
        return _one(base, src, spec, symbol, request, date_expression)

    try:
        for request, entry in _results(requests, fetch, concurrency(src, workers)):
            landed(request, entry)
    finally:
        ledger.record(base, pending)
        report.seconds = time.perf_counter() - started
    return report


def _results(
    requests: Sequence[Request],
    fetch: Callable[[Request], ledger.Entry],
    workers: int,
) -> Iterator[tuple[Request, ledger.Entry]]:
    """Each request's entry, at most `workers` in flight, first failure wins.

    Submission is bounded rather than "submit everything and iterate", and on a
    metered plan that is the whole point: when one request fails, the ones not
    yet dispatched are never paid for. Work already in flight is allowed to
    finish and is still recorded -- a request that has been sent has been
    billed, and throwing its answer away helps nobody.
    """
    if workers <= 1:
        for request in requests:
            yield request, fetch(request)
        return

    failure: BaseException | None = None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        stream = iter(requests)
        queue: deque[tuple[Request, Future[ledger.Entry]]] = deque()
        while True:
            while failure is None and len(queue) < workers:
                nxt = next(stream, None)
                if nxt is None:
                    break
                queue.append((nxt, pool.submit(fetch, nxt)))
            if not queue:
                break
            request, future = queue.popleft()
            try:
                yield request, future.result()
            except BaseException as exc:  # noqa: BLE001 - re-raised once drained
                failure = failure or exc
    if failure is not None:
        raise failure


def _one(
    base: Path,
    src: HistorySource,
    spec: TableSpec,
    symbol: str,
    request: Request,
    date_expression: str | None,
) -> ledger.Entry:
    """One vendor call, written and recorded. Never raises for a vendor refusal."""
    try:
        frame = src.fetch(spec.name, symbol, request.window, request.scope)
    except NotEntitled as exc:
        # Permanent. Recorded so this window is never paid for again, and kept
        # out of `covered` so nothing claims the data is held.
        return ledger.Entry(
            src.name,
            spec.name,
            symbol,
            request.scope.key,
            request.window,
            0,
            ledger.DENIED,
            str(exc),
        )
    if frame is None or frame.num_rows == 0:
        # The vendor answered and had nothing: a holiday, or a contract that
        # never traded. Settled, and never asked for again.
        return ledger.Entry(
            src.name, spec.name, symbol, request.scope.key, request.window, 0, ledger.EMPTY
        )
    rows = store.write(base, src.name, spec, frame, date_expression)
    return ledger.Entry(
        src.name, spec.name, symbol, request.scope.key, request.window, rows, ledger.OK
    )
