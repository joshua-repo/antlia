"""`describe()` -- the answers, including the ones that are not failures."""

from __future__ import annotations

import pytest

from antlia import gateway
from antlia.auth.errors import UnknownSource
from tests.gateway.fakes import FakeIBC, dead_port


def test_a_source_with_no_gateway_describes_as_none():
    # The whole protocol for "there is nothing to look at". A consumer branches
    # on this instead of keeping its own list of which sources have a screen.
    assert gateway.describe("trading212", "live") is None


def test_an_unknown_name_raises_rather_than_reporting_no_gateway():
    # A typo must not pass as "this source has no gateway" -- that answer is
    # indistinguishable from the truth and would never be noticed.
    with pytest.raises(UnknownSource):
        gateway.describe("ibkrr")


def test_the_defaults_are_the_published_ports():
    info = gateway.describe("ibkr", "live")
    assert info is not None
    assert info.screen_url == "http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale"
    assert info.vnc_addr == "127.0.0.1:5900"
    assert info.control_addr == "127.0.0.1:7462"
    assert info.source == "ibkr" and info.profile == "live"


def test_control_is_false_when_nothing_is_listening(monkeypatch):
    # The normal state of a stock gateway: IBC ships CommandServerPort=0. It is
    # an answer, not an error, and describe() must still return.
    monkeypatch.setenv("ANTLIA_IBKR_CONTROL_PORT", str(dead_port()))
    info = gateway.describe("ibkr", "live")
    assert info is not None and info.control is False
    assert info.control_addr is not None


def test_control_is_true_when_something_answers(monkeypatch):
    with FakeIBC() as ibc:
        monkeypatch.setenv("ANTLIA_IBKR_CONTROL_PORT", str(ibc.port))
        info = gateway.describe("ibkr", "live")
    assert info is not None and info.control is True
    # The probe connects and closes. It must not send anything, or a describe
    # would be able to disturb a gateway mid-login.
    assert ibc.commands == []


def test_a_zero_port_means_not_published(monkeypatch):
    monkeypatch.setenv("ANTLIA_IBKR_VNC_PORT", "0")
    monkeypatch.setenv("ANTLIA_IBKR_SCREEN_PORT", "0")
    monkeypatch.setenv("ANTLIA_IBKR_CONTROL_PORT", "0")
    info = gateway.describe("ibkr", "live")
    assert info is not None
    assert (info.screen_url, info.vnc_addr, info.control_addr) == (None, None, None)
    assert info.control is False


def test_host_is_shared_with_auth_so_a_remote_gateway_is_one_edit(config):
    # `host` is the same field auth resolves for the API socket. A gateway on
    # another machine moves all four endpoints at once.
    config("[ibkr]\nhost = '10.0.0.5'\n")
    info = gateway.describe("ibkr", "live")
    assert info is not None
    assert info.screen_url is not None and info.screen_url.startswith("http://10.0.0.5:6080/")
    assert info.vnc_addr == "10.0.0.5:5900"
    assert info.control_addr == "10.0.0.5:7462"


def test_a_whole_url_can_be_supplied_and_keeps_its_own_parameters(config):
    config("[ibkr]\nscreen_url = 'https://gw.example/novnc/vnc.html?resize=remote'\n")
    info = gateway.describe("ibkr", "live")
    assert info is not None and info.screen_url is not None
    # The caller's resize= wins; ours is only a default.
    assert "resize=remote" in info.screen_url
    assert "resize=scale" not in info.screen_url
    assert "autoconnect=1" in info.screen_url


def test_describe_opens_no_session():
    from antlia.auth import pool

    gateway.describe("ibkr", "live")
    # Looking at a gateway must not be a reason to authenticate to the broker
    # behind it -- the point of this module is that it works when that fails.
    assert pool.open_sessions() == []
