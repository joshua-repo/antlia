"""`restart()` and `command()` -- the reply, and the two ways it fails."""

from __future__ import annotations

import pytest

from antlia import gateway
from antlia.auth.errors import ConnectionFailed
from antlia.gateway.errors import ControlUnavailable
from tests.gateway.fakes import FakeIBC, dead_port


def test_restart_sends_restart_and_returns_the_reply_verbatim(monkeypatch):
    with FakeIBC(reply="OK Setting auto-restart time to 11:42 AM") as ibc:
        monkeypatch.setenv("ANTLIA_IBKR_CONTROL_PORT", str(ibc.port))
        reply = gateway.restart("ibkr", "live")
    assert ibc.commands == ["RESTART"]
    # Verbatim: IBC's replies are a person's sentence, and nothing in antlia or
    # in a consumer parses them.
    assert reply == "OK Setting auto-restart time to 11:42 AM"


def test_an_arbitrary_command_goes_through_unchanged(monkeypatch):
    with FakeIBC(reply="OK") as ibc:
        monkeypatch.setenv("ANTLIA_IBKR_CONTROL_PORT", str(ibc.port))
        assert gateway.command("ibkr", "RECONNECTDATA", "live") == "OK"
    assert ibc.commands == ["RECONNECTDATA"]


def test_a_silent_reply_is_still_a_success(monkeypatch):
    with FakeIBC(reply="") as ibc:
        monkeypatch.setenv("ANTLIA_IBKR_CONTROL_PORT", str(ibc.port))
        assert gateway.restart("ibkr", "live") == "(no reply)"


def test_a_refused_connection_raises_and_says_why(monkeypatch):
    monkeypatch.setenv("ANTLIA_IBKR_CONTROL_PORT", str(dead_port()))
    with pytest.raises(ControlUnavailable) as caught:
        gateway.restart("ibkr", "live")
    message = str(caught.value)
    # Prose, but it has to name the thing the reader can go and change.
    assert "CommandServerPort=0" in message
    assert "override" in message


def test_it_is_still_a_connectionfailed(monkeypatch):
    # The contract consumers were promised: no new import to keep working.
    monkeypatch.setenv("ANTLIA_IBKR_CONTROL_PORT", str(dead_port()))
    with pytest.raises(ConnectionFailed):
        gateway.restart("ibkr", "live")


def test_a_wedged_gateway_times_out_and_says_what_that_means(monkeypatch):
    # IBC alive but waiting on a modal dialog: the socket accepts and nothing
    # comes back. This is the failure a refusal must not be confused with.
    with FakeIBC(reply=None) as ibc:
        monkeypatch.setenv("ANTLIA_IBKR_CONTROL_PORT", str(ibc.port))
        monkeypatch.setenv("ANTLIA_IBKR_CONTROL_TIMEOUT", "0.3")
        with pytest.raises(ControlUnavailable) as caught:
            gateway.restart("ibkr", "live")
    assert "did not answer" in str(caught.value)
    assert "modal" in str(caught.value)


def test_an_unconfigured_control_port_raises_rather_than_dialling_zero(monkeypatch):
    monkeypatch.setenv("ANTLIA_IBKR_CONTROL_PORT", "0")
    with pytest.raises(ControlUnavailable) as caught:
        gateway.restart("ibkr", "live")
    assert "no control port is configured" in str(caught.value)


def test_a_source_with_no_gateway_cannot_be_commanded():
    with pytest.raises(ControlUnavailable) as caught:
        gateway.restart("trading212", "live")
    assert "has no gateway" in str(caught.value)
