from __future__ import annotations

import contextlib

import pytest

from antlia import auth
from antlia.auth.base import Provider
from antlia.auth.errors import ConnectionFailed, MissingExtra, UnknownSource
from antlia.auth.spec import Field, SourceSpec


class FakeClient:
    def __init__(self, cred, limiter):
        self.cred = cred
        self.limiter = limiter
        self.alive = True
        self.closed = 0

    def close(self):
        self.alive = False
        self.closed += 1


class FakeProvider(Provider):
    spec = SourceSpec(
        name="fake",
        fields=(
            Field("api_key", secret=True),
            Field("port", required=False, default=1, cast=int),
            Field("rate_limit", required=False, cast=float),
        ),
        profiles=True,
        default_profile="live",
        identity=("api_key", "port"),
        rate=5.0,
    )

    def __init__(self):
        self.connects = 0

    def connect(self, cred, limiter):
        self.connects += 1
        return FakeClient(cred, limiter)

    def disconnect(self, handle):
        handle.close()

    def healthy(self, handle):
        return handle.alive


@pytest.fixture
def fake(monkeypatch, config):
    config('[fake]\napi_key = "k"\n')
    provider = FakeProvider()
    auth.register("fake", provider)
    return provider


def test_session_yields_the_vendor_client(fake):
    with auth.session("fake") as client:
        assert isinstance(client, FakeClient)
        assert client.cred["api_key"] == "k"


def test_same_identity_reuses_one_connection(fake):
    with auth.session("fake") as a, auth.session("fake") as b:
        assert a is b
    assert fake.connects == 1


def test_connection_outlives_the_with_block(fake):
    with auth.session("fake") as first:
        pass
    with auth.session("fake") as second:
        assert second is first
    assert fake.connects == 1


def test_different_identity_gets_its_own_connection(fake):
    with auth.session("fake") as a, auth.session("fake", port=2) as b:
        assert a is not b
    assert fake.connects == 2


def test_dead_handle_is_replaced(fake):
    with auth.session("fake") as first:
        first.alive = False
    with auth.session("fake") as second:
        assert second is not first
    assert fake.connects == 2


def test_close_disconnects_and_close_is_idempotent(fake):
    with auth.session("fake") as client:
        pass
    assert auth.close("fake") == 1
    assert client.closed == 1
    assert auth.close("fake") == 0


def test_close_leaves_a_session_that_is_in_use(fake):
    with auth.session("fake") as client:
        assert auth.close("fake") == 0
        assert client.alive


def test_close_all_takes_everything(fake):
    with auth.session("fake"), auth.session("fake", port=2):
        pass
    assert auth.close_all() == 2
    assert auth.open_sessions() == []


def test_open_sessions_reports_refcounts(fake):
    with auth.session("fake"):
        assert auth.open_sessions() == [("fake", "live", 1)]


def test_credential_does_not_connect(fake):
    cred = auth.credential("fake")
    assert cred["api_key"] == "k"
    assert fake.connects == 0


def test_adapter_exceptions_surface_as_connection_failed(monkeypatch, fake):
    monkeypatch.setattr(
        FakeProvider, "connect", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with pytest.raises(ConnectionFailed, match="boom"), auth.session("fake"):
        pass


def test_rate_limit_from_config_overrides_the_spec(config):
    config('[fake]\napi_key = "k"\nrate_limit = 0.25\n')
    auth.register("fake", FakeProvider())
    with auth.session("fake") as client:
        assert client.limiter.rate == 0.25


def test_limiter_available_without_credentials():
    auth.register("fake", FakeProvider())
    assert auth.limiter("fake").rate == 5.0


def test_unknown_source_lists_what_is_registered():
    with pytest.raises(UnknownSource, match="ibkr"):
        auth.credential("nope")


def test_missing_sdk_names_the_extra():
    class Needy(Provider):
        spec = SourceSpec(name="needy", extra="needy", package="no_such_pkg")

        def connect(self, cred, limiter):
            return self.require("no_such_pkg")

    auth.register("needy", Needy())
    with pytest.raises(MissingExtra, match=r"antlia\[needy\]"), auth.session("needy"):
        pass


class TestThreadSafety:
    """Pooling plus threads: a shared single-threaded client must be queued."""

    def source(self, thread_safe):
        class Provider(auth.Provider):
            spec = auth.SourceSpec(
                name="single",
                fields=(auth.Field("api_key", secret=True),),
                identity=("api_key",),
                thread_safe=thread_safe,
            )

            def connect(self, cred, limiter):
                return object()

        auth.register("single", Provider())

    def overlap(self, name):
        """How many threads were inside the with-block simultaneously."""
        import threading

        inside = 0
        peak = 0
        guard = threading.Lock()
        barrier = threading.Barrier(4, timeout=5)

        def worker():
            nonlocal inside, peak
            with auth.session(name):
                with guard:
                    inside += 1
                    peak = max(peak, inside)
                # Serialised sources never all arrive; the timeout is the
                # expected outcome there, not a failure.
                with contextlib.suppress(threading.BrokenBarrierError):
                    barrier.wait()
                with guard:
                    inside -= 1

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        return peak

    def test_a_thread_safe_source_is_shared_freely(self, config):
        config('[single]\napi_key = "k"\n')
        self.source(thread_safe=True)
        assert self.overlap("single") > 1

    def test_a_single_threaded_source_is_held_exclusively(self, config):
        # Four concurrent IBKR snapshots deadlocked before this; serialising
        # turns that into a queue.
        config('[single]\napi_key = "k"\n')
        self.source(thread_safe=False)
        assert self.overlap("single") == 1

    def test_nesting_on_one_thread_still_works(self, config):
        # The lock is re-entrant: a plain Lock would deadlock a caller that
        # opens a session inside another one.
        config('[single]\napi_key = "k"\n')
        self.source(thread_safe=False)
        with auth.session("single") as outer, auth.session("single") as inner:
            assert outer is inner

    def test_ibkr_declares_itself_single_threaded(self):
        assert auth.provider("ibkr").spec.thread_safe is False
        assert auth.provider("trading212").spec.thread_safe is True
