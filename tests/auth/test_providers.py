"""Provider specs and the pure parts of the adapters.

Nothing here opens a connection: these test the decisions the adapters make
before touching the network, which is where the boundary rules live.
"""

from __future__ import annotations

import sys

import pytest

from antlia import auth
from antlia.auth import registry
from antlia.auth.errors import ConnectionFailed, MissingCredential
from antlia.auth.providers.ibkr import IBKRProvider
from antlia.auth.providers.thetadata import ThetaDataProvider
from antlia.auth.providers.trading212 import Trading212Provider


def cred_for(provider, profile=None, **overrides):
    from antlia.auth.credentials import resolve

    return resolve(provider.spec, profile, overrides)


class TestRegistry:
    def test_builtin_sources_are_all_registered(self):
        assert set(registry.sources()) >= {"ibkr", "thetadata", "trading212", "yfinance"}

    def test_listing_sources_imports_no_adapter(self):
        for name in list(sys.modules):
            if name.startswith("antlia.auth.providers."):
                del sys.modules[name]
        registry.reset()
        registry.sources()
        assert not [n for n in sys.modules if n.startswith("antlia.auth.providers.")]

    def test_custom_source_can_be_registered(self):
        class Mine(auth.Provider):
            spec = auth.SourceSpec(name="mine")

            def connect(self, cred, limiter):
                return object()

        auth.register("mine", Mine())
        assert "mine" in registry.sources()
        auth.unregister("mine")
        assert "mine" not in registry.sources()


class TestIBKR:
    provider = IBKRProvider()

    def test_readonly_by_default(self):
        # The "no execution path in antlia" rule, enforced by the gateway.
        assert cred_for(self.provider)["readonly"] is True

    def test_profile_picks_the_port(self):
        assert self.provider.port_for(cred_for(self.provider, "paper")) == 7497
        assert self.provider.port_for(cred_for(self.provider, "live")) == 7496

    def test_explicit_port_wins_over_profile(self):
        assert self.provider.port_for(cred_for(self.provider, "paper", port=4001)) == 4001

    def test_unknown_profile_without_a_port_is_a_clear_failure(self, config):
        config("[ibkr.gw]\n")
        with pytest.raises(ConnectionFailed, match="no port for profile"):
            self.provider.port_for(cred_for(self.provider, "gw"))

    def test_client_ids_are_allocated_distinctly(self):
        cred = cred_for(self.provider, "paper")
        assert self.provider.client_id(cred) != self.provider.client_id(cred)

    def test_fixed_client_id_is_respected(self):
        cred = cred_for(self.provider, "paper", client_id=42)
        assert self.provider.client_id(cred) == 42

    def test_client_id_is_not_part_of_session_identity(self):
        # Otherwise auto-allocation would open a new gateway session per call.
        assert "client_id" not in self.provider.spec.identity

    def test_dead_handle_reports_unhealthy(self):
        class Broken:
            def isConnected(self):
                raise OSError("socket gone")

        assert self.provider.healthy(Broken()) is False


class TestTrading212:
    provider = Trading212Provider()

    def test_profile_picks_the_host(self):
        assert self.provider.base_url(cred_for(self.provider, "live", api_key="k")) == (
            "https://live.trading212.com"
        )
        assert self.provider.base_url(cred_for(self.provider, "demo", api_key="k")) == (
            "https://demo.trading212.com"
        )

    def test_explicit_base_url_wins_and_loses_its_slash(self):
        cred = cred_for(self.provider, "live", api_key="k", base_url="https://x.test/")
        assert self.provider.base_url(cred) == "https://x.test"

    def test_api_key_is_secret(self):
        cred = cred_for(self.provider, "live", api_key="tttt")
        assert "tttt" not in repr(cred)


class TestSpecs:
    @pytest.mark.parametrize("name", ["ibkr", "thetadata", "trading212", "yfinance"])
    def test_every_source_declares_its_extra_and_package(self, name):
        spec = registry.get(name).spec
        assert spec.extra and spec.package

    @pytest.mark.parametrize("name", ["ibkr", "thetadata", "trading212", "yfinance"])
    def test_every_source_is_tunable_at_runtime(self, name):
        assert "rate_limit" in registry.get(name).spec.field_map()

    @pytest.mark.parametrize("name", ["ibkr", "trading212"])
    def test_profiled_sources_declare_a_default_profile(self, name):
        spec = registry.get(name).spec
        assert spec.profiles and spec.default_profile


class TestVerify:
    """`connect()` succeeding is not evidence. These are the round-trips."""

    def test_ibkr_verify_needs_a_managed_account(self):
        provider = IBKRProvider()

        class Gateway:
            def __init__(self, accounts):
                self.accounts = accounts
                self.client = type("C", (), {"serverVersion": lambda self: 176})()

            def isConnected(self):
                return True

            def managedAccounts(self):
                return self.accounts

        assert "DU123" in provider.verify(Gateway(["DU123"]))
        # Connected but still logging in: the socket is up and the session is
        # useless, which is exactly the state a bare connect() calls success.
        with pytest.raises(ConnectionFailed, match="manages no accounts"):
            provider.verify(Gateway([]))

    def test_ibkr_verify_rejects_a_dead_socket(self):
        class Dead:
            def isConnected(self):
                return False

        with pytest.raises(ConnectionFailed, match="not connected"):
            IBKRProvider().verify(Dead())

    @pytest.mark.parametrize(
        ("status", "message"),
        [(401, "not valid"), (403, "scope"), (429, "rate limited"), (500, "HTTP 500")],
    )
    def test_trading212_verify_explains_each_refusal(self, status, message):
        class Response:
            status_code = status

            def json(self):
                return {}

        class Client:
            def get(self, path):
                return Response()

        with pytest.raises(ConnectionFailed, match=message):
            Trading212Provider().verify(Client())

    def test_trading212_verify_reports_the_account_on_success(self):
        class Client:
            def get(self, path):
                return type(
                    "R",
                    (),
                    {
                        "status_code": 200,
                        "json": lambda self: {"id": 42, "currencyCode": "GBP"},
                    },
                )()

        assert Trading212Provider().verify(Client()) == "account 42 (GBP)"

    def test_thetadata_verify_rejects_an_unauthenticated_client(self):
        class Client:
            auth_token = None
            mdds_host = "mdds-01.thetadata.us"

        with pytest.raises(ConnectionFailed, match="no auth token"):
            ThetaDataProvider().verify(Client())

    def test_thetadata_verify_separates_auth_from_the_data_channel(self):
        class Client:
            auth_token = "t"
            mdds_host = "mdds-01.thetadata.us"
            mdds_port = "443"

            def option_list_expirations(self, symbol):
                raise OSError("channel closed")

        # The distinction matters: the key is fine, the transport is not, and
        # being told "authentication failed" would send you to the wrong place.
        with pytest.raises(ConnectionFailed, match="authenticated, but the data channel failed"):
            ThetaDataProvider().verify(Client())

    def test_thetadata_verify_reports_the_expiration_count(self):
        class Client:
            auth_token = "t"
            mdds_host = "mdds-01.thetadata.us"

            def option_list_expirations(self, symbol):
                return ["2026-09-18", "2026-10-16"]

        assert "2 expirations" in ThetaDataProvider().verify(Client())

    def test_a_source_without_a_verify_says_so(self):
        class Bare(auth.Provider):
            spec = auth.SourceSpec(name="bare")

            def connect(self, cred, limiter):
                return object()

        assert "no verification" in Bare().verify(object())


class TestThetaDataCredentialShapes:
    """api_key OR email+password OR creds_file -- not expressible per field."""

    provider = ThetaDataProvider()

    @pytest.mark.parametrize(
        "given",
        [
            {"api_key": "k"},
            {"email": "a@b.c", "password": "p"},
            {"creds_file": "/tmp/creds.json"},
        ],
    )
    def test_each_accepted_shape_passes_the_check(self, given):
        self.provider._check_one_of(cred_for(self.provider, **given))

    @pytest.mark.parametrize("given", [{}, {"email": "a@b.c"}, {"password": "p"}])
    def test_incomplete_shapes_name_all_three_options(self, given):
        with pytest.raises(MissingCredential) as exc:
            self.provider._check_one_of(cred_for(self.provider, **given))
        message = str(exc.value)
        assert "ANTLIA_THETADATA_API_KEY" in message
        assert "creds_file" in message

    def test_kwargs_are_filtered_against_the_installed_signature(self):
        def ctor(self, api_key=None, email=None, password=None, mdds_host=None): ...

        cred = cred_for(self.provider, api_key="k", dataframe_type="pandas", mdds_port="443")
        kwargs = self.provider._kwargs(ctor, cred)
        assert kwargs == {"api_key": "k"}  # unknown keywords dropped, not passed
