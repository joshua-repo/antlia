from __future__ import annotations

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
