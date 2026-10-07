"""The VNC password: filled in for use, absent from anything that is kept."""

from __future__ import annotations

from antlia import gateway
from antlia.gateway.sources.ibkr import VNC_PASSWORD_ENV


def test_no_password_configured_means_novnc_will_ask():
    info = gateway.describe("ibkr", "live")
    assert info is not None
    assert info.screen_prefilled is False
    assert info.screen_url is not None and "password" not in info.screen_url


def test_the_gateway_projects_own_variable_supplies_it(monkeypatch):
    # `set -a; . ~/ib-gateway/.env; set +a` is the whole contract -- the same
    # shell line that starts the container also supplies this.
    monkeypatch.setenv(VNC_PASSWORD_ENV, "a b$c")
    info = gateway.describe("ibkr", "live")
    assert info is not None
    assert info.screen_prefilled is True
    assert "password=a+b%24c" in (info.screen_url or "")


def test_antlias_own_spelling_wins_over_the_bare_variable(monkeypatch):
    monkeypatch.setenv(VNC_PASSWORD_ENV, "from-the-gateway-project")
    monkeypatch.setenv("ANTLIA_IBKR_LIVE_VNC_PASSWORD", "from-antlia")
    info = gateway.describe("ibkr", "live")
    assert info is not None and "password=from-antlia" in (info.screen_url or "")


def test_the_vendors_spelling_is_accepted_in_the_config_file(monkeypatch, config):
    # Someone copying the name off the compose file writes VNC_SERVER_PASSWORD.
    config("[ibkr.live]\nVNC_SERVER_PASSWORD = 'shh'\n")
    info = gateway.describe("ibkr", "live")
    assert info is not None and "password=shh" in (info.screen_url or "")


def test_nothing_that_can_be_kept_carries_the_password(monkeypatch):
    monkeypatch.setenv(VNC_PASSWORD_ENV, "hunter2")
    info = gateway.describe("ibkr", "live")
    assert info is not None

    # The only way to the secret is asking for the attribute by name. Every
    # accidental route -- a log line, a repr, a serialised payload -- is safe,
    # because the redaction is in the type rather than in each caller.
    assert "hunter2" in (info.screen_url or "")
    assert "hunter2" not in repr(info)
    assert "hunter2" not in str(info.redacted())
    assert "hunter2" not in (info.safe_screen_url or "")
    # And it is still a working link, just one that asks.
    assert "autoconnect=1" in (info.safe_screen_url or "")


def test_a_password_already_in_a_supplied_url_is_replaced_not_doubled(monkeypatch, config):
    config("[ibkr]\nscreen_url = 'http://gw:6080/vnc.html?password=stale'\n")
    monkeypatch.setenv(VNC_PASSWORD_ENV, "fresh")
    info = gateway.describe("ibkr", "live")
    assert info is not None and info.screen_url is not None
    assert info.screen_url.count("password=") == 1
    assert "password=fresh" in info.screen_url
