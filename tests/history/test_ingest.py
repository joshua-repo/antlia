"""The planner. Every assertion here is a vendor request that is not made."""

from __future__ import annotations

import datetime as dt
import time

import pytest

from antlia.history import ingest, ledger
from antlia.history.types import EQUITY_EOD, OPTION_EOD, Window

from .fakes import FakeHistory

D = dt.date
JAN = Window(D(2026, 1, 1), D(2026, 1, 31))


def test_the_plan_splits_by_the_vendors_span_cap(tmp_path):
    src = FakeHistory()
    plan = ingest.plan(tmp_path, src, EQUITY_EOD, "AAPL", JAN)
    assert plan.calls == 4
    assert all(r.window.days <= src.max_span_days for r in plan.requests)


def test_the_plan_clamps_to_the_entitlement_horizon(tmp_path):
    src = FakeHistory(earliest=D(2026, 1, 15))
    plan = ingest.plan(tmp_path, src, EQUITY_EOD, "AAPL", JAN)
    assert str(plan.beyond_horizon) == "2026-01-01 .. 2026-01-14"
    assert min(r.window.start for r in plan.requests) == D(2026, 1, 15)


def test_a_window_entirely_before_the_horizon_asks_for_nothing(tmp_path):
    src = FakeHistory(earliest=D(2026, 6, 1))
    plan = ingest.plan(tmp_path, src, EQUITY_EOD, "AAPL", JAN)
    assert plan.requests == () and plan.eligible is None


def test_a_second_run_over_a_covered_window_costs_no_requests(tmp_path):
    src = FakeHistory()
    ingest.run(tmp_path, src, EQUITY_EOD, "AAPL", JAN)
    made = len(src.calls)
    report = ingest.run(tmp_path, src, EQUITY_EOD, "AAPL", JAN)
    assert report.calls == 0
    assert len(src.calls) == made


def test_only_the_gap_between_two_runs_is_fetched(tmp_path):
    src = FakeHistory()
    ingest.run(tmp_path, src, EQUITY_EOD, "AAPL", Window(D(2026, 1, 1), D(2026, 1, 10)))
    src.calls.clear()
    ingest.run(tmp_path, src, EQUITY_EOD, "AAPL", JAN)
    assert min(w.start for _, _, w, _ in src.calls) == D(2026, 1, 11)


def test_an_empty_answer_is_recorded_and_never_asked_for_again(tmp_path):
    src = FakeHistory(blank=True)
    report = ingest.run(tmp_path, src, EQUITY_EOD, "AAPL", JAN)
    assert report.rows == 0 and report.empty == report.calls
    src.calls.clear()
    assert ingest.run(tmp_path, src, EQUITY_EOD, "AAPL", JAN).calls == 0
    assert src.calls == []


def test_a_denial_is_recorded_and_never_retried(tmp_path):
    src = FakeHistory(deny=True)
    report = ingest.run(tmp_path, src, EQUITY_EOD, "AAPL", JAN)
    assert report.denied == report.calls > 0
    # Refused is not covered -- but it is settled, so it is not re-requested.
    assert ledger.windows(tmp_path, "fake", "equity_eod", "AAPL") == []
    src.calls.clear()
    assert ingest.run(tmp_path, src, EQUITY_EOD, "AAPL", JAN).calls == 0


def test_an_option_scope_is_clamped_to_its_own_life(tmp_path):
    # The January expiration must never be asked about February. On a
    # multi-year warm-up this is the single largest saving there is.
    src = FakeHistory()
    ingest.run(tmp_path, src, OPTION_EOD, "AAPL", Window(D(2026, 1, 1), D(2026, 2, 28)))
    january = [w for _, _, w, key in src.calls if key == "2026-01-16"]
    assert max(w.end for w in january) == D(2026, 1, 16)


def test_max_dte_drops_whole_expirations_from_the_plan(tmp_path):
    src = FakeHistory()
    plan = ingest.plan(
        tmp_path, src, OPTION_EOD, "AAPL", Window(D(2026, 1, 1), D(2026, 1, 10)), max_dte=10
    )
    assert {r.scope.key for r in plan.requests} == {"2026-01-16"}


def test_limit_caps_the_requests_and_the_next_run_resumes(tmp_path):
    src = FakeHistory()
    first = ingest.run(tmp_path, src, EQUITY_EOD, "AAPL", JAN, limit=2)
    assert first.calls == 2
    second = ingest.run(tmp_path, src, EQUITY_EOD, "AAPL", JAN)
    assert second.calls == 2
    assert ingest.run(tmp_path, src, EQUITY_EOD, "AAPL", JAN).calls == 0


def test_progress_sees_every_entry_as_it_lands(tmp_path):
    src = FakeHistory()
    seen: list[str] = []
    ingest.run(tmp_path, src, EQUITY_EOD, "AAPL", JAN, progress=lambda r, e: seen.append(e.status))
    # The last chunk is a lone Saturday, so it settles as `empty` -- which is
    # exactly the status a holiday gets, and exactly why it is not a failure.
    assert seen == [ledger.OK, ledger.OK, ledger.OK, ledger.EMPTY]


def test_an_interrupted_run_keeps_what_it_paid_for(tmp_path):
    class Boom(FakeHistory):
        def fetch(self, table, symbol, window, scope):
            if len(self.calls) >= 2:
                raise RuntimeError("connection reset")
            return super().fetch(table, symbol, window, scope)

    src = Boom()
    with pytest.raises(RuntimeError):
        ingest.run(tmp_path, src, EQUITY_EOD, "AAPL", JAN)
    # The two requests that succeeded are recorded, so the retry is cheaper.
    assert len(ledger.windows(tmp_path, "fake", "equity_eod", "AAPL")) == 1
    assert ingest.plan(tmp_path, src, EQUITY_EOD, "AAPL", JAN).calls == 2


def test_a_slow_run_records_progress_before_it_is_killed(tmp_path, monkeypatch):
    # A SIGTERM never reaches the `finally`, so the buffer has to be bounded in
    # time as well as in count. Measured: twenty minutes of fetched chains
    # landed in `raw/` with an empty ledger, and the retry paid for all of it.
    monkeypatch.setattr(ingest, "FLUSH_SECONDS", 0.0)
    src = FakeHistory()
    seen: list[int] = []

    def watch(request, entry):
        seen.append(len(ledger.windows(tmp_path, "fake", "equity_eod", "AAPL")))

    ingest.run(tmp_path, src, EQUITY_EOD, "AAPL", JAN, progress=watch)
    # The first entry is already on disk by the time the second is fetched.
    assert seen[1] >= 1


def test_refresh_replans_a_window_the_ledger_already_settles(tmp_path):
    src = FakeHistory()
    ingest.run(tmp_path, src, EQUITY_EOD, "AAPL", JAN)
    assert ingest.plan(tmp_path, src, EQUITY_EOD, "AAPL", JAN).calls == 0
    assert ingest.plan(tmp_path, src, EQUITY_EOD, "AAPL", JAN, refresh=True).calls == 4


def test_refresh_also_retries_what_was_denied(tmp_path):
    # An upgraded subscription is exactly when a permanent refusal stops being
    # permanent, and it is the only moment anyone wants to ask again.
    denied = FakeHistory(deny=True)
    ingest.run(tmp_path, denied, EQUITY_EOD, "AAPL", JAN)
    assert ingest.plan(tmp_path, denied, EQUITY_EOD, "AAPL", JAN).calls == 0

    allowed = FakeHistory()
    report = ingest.run(tmp_path, allowed, EQUITY_EOD, "AAPL", JAN, refresh=True)
    assert report.calls == 4 and report.rows > 0


def test_workers_fetch_the_same_thing_as_one_worker(tmp_path):
    one, many = FakeHistory(), FakeHistory()
    solo = ingest.run(tmp_path / "a", one, EQUITY_EOD, "AAPL", JAN)
    pooled = ingest.run(tmp_path / "b", many, EQUITY_EOD, "AAPL", JAN, workers=4)
    assert (pooled.calls, pooled.rows, pooled.empty) == (solo.calls, solo.rows, solo.empty)
    assert sorted(w.start for _, _, w, _ in many.calls) == sorted(
        w.start for _, _, w, _ in one.calls
    )


def test_a_parallel_run_records_everything_it_fetched(tmp_path):
    src = FakeHistory()
    ingest.run(tmp_path, src, EQUITY_EOD, "AAPL", JAN, workers=4)
    # The ledger is written from several threads; nothing may be dropped.
    assert [str(w) for w in ledger.windows(tmp_path, "fake", "equity_eod", "AAPL")] == [
        "2026-01-01 .. 2026-01-31"
    ]


def test_a_failure_stops_dispatching_but_keeps_what_was_paid_for(tmp_path):
    class Flaky(FakeHistory):
        def fetch(self, table, symbol, window, scope):
            if len(self.calls) >= 2:
                raise RuntimeError("connection reset")
            return super().fetch(table, symbol, window, scope)

    src = Flaky()
    with pytest.raises(RuntimeError):
        ingest.run(tmp_path, src, EQUITY_EOD, "AAPL", JAN, workers=4)
    # At most the in-flight batch was sent, never the whole plan, and what
    # succeeded is on disk so the retry is cheaper.
    assert len(src.calls) <= 4
    assert ledger.windows(tmp_path, "fake", "equity_eod", "AAPL")


def test_workers_are_clamped_for_a_single_threaded_sdk(tmp_path, monkeypatch):
    from antlia.auth import registry as auth_registry
    from antlia.auth.base import Provider
    from antlia.auth.spec import SourceSpec

    class Solo(Provider):
        spec = SourceSpec(name="solo", thread_safe=False)

        def connect(self, cred, limiter):
            raise NotImplementedError

    auth_registry.register("solo", Solo())
    src = FakeHistory()
    src.auth_source = "solo"
    # Pooling plus threads on a single-threaded client is a deadlock, not a
    # race, and `auth` is where that fact lives.
    assert ingest.concurrency(src, 8) == 1
    src.auth_source = None
    assert ingest.concurrency(src, 8) == 8


def test_workers_actually_overlap(tmp_path):
    # The guard against a `workers=` that quietly does nothing: this asserts
    # concurrency observably, not just that the results match. It was written
    # because the first implementation of `workers` was swallowed by `**options`
    # and every "parallel" test passed while running in sequence.
    import threading as _threading

    class Slow(FakeHistory):
        def __init__(self):
            super().__init__()
            self.inside = 0
            self.peak = 0
            self.gate = _threading.Lock()

        def fetch(self, table, symbol, window, scope):
            with self.gate:
                self.inside += 1
                self.peak = max(self.peak, self.inside)
            try:
                time.sleep(0.05)
                return super().fetch(table, symbol, window, scope)
            finally:
                with self.gate:
                    self.inside -= 1

    solo = Slow()
    ingest.run(tmp_path / "one", solo, EQUITY_EOD, "AAPL", JAN, workers=1)
    assert solo.peak == 1

    pooled = Slow()
    ingest.run(tmp_path / "many", pooled, EQUITY_EOD, "AAPL", JAN, workers=4)
    assert pooled.peak > 1


def test_the_vendors_ceiling_clamps_a_greedy_caller(tmp_path):
    # A vendor concurrency limit is a rule, not a preference: ThetaData refuses
    # the third simultaneous request rather than queueing it. Asking for eight
    # must therefore quietly become the vendor's number, exactly as a 400-day
    # window quietly becomes 365-day chunks.
    src = FakeHistory()
    src.max_workers = 2
    assert ingest.concurrency(src, 8) == 2
    assert ingest.concurrency(src, 1) == 1
