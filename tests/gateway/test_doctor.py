"""`python -m antlia.gateway` -- and the one thing it must never print."""

from __future__ import annotations

import json

from antlia.gateway.__main__ import main
from antlia.gateway.sources.ibkr import VNC_PASSWORD_ENV
from tests.gateway.fakes import FakeIBC, dead_port


def test_it_lists_what_is_configured(monkeypatch, capsys):
    monkeypatch.setenv("ANTLIA_IBKR_CONTROL_PORT", str(dead_port()))
    assert main(["-p", "live"]) == 0
    out = capsys.readouterr().out
    assert "screen" in out and "control" in out
    # A dead control channel is a normal state, so it is reported, not failed.
    assert "no answer" in out
    assert "CommandServerPort=0" in out


def test_a_live_control_channel_reads_as_answering(monkeypatch, capsys):
    with FakeIBC() as ibc:
        monkeypatch.setenv("ANTLIA_IBKR_CONTROL_PORT", str(ibc.port))
        assert main(["-s", "ibkr", "-p", "live"]) == 0
    assert "answers" in capsys.readouterr().out


def test_a_source_with_no_gateway_says_so_when_asked_for_by_name(capsys):
    assert main(["-s", "trading212"]) == 0
    assert "no gateway" in capsys.readouterr().out


def test_the_doctor_never_prints_the_password(monkeypatch, capsys):
    # A terminal transcript gets pasted into an issue; a --json run gets
    # redirected into a file. Both have to be safe by construction.
    monkeypatch.setenv(VNC_PASSWORD_ENV, "hunter2")
    assert main(["-p", "live"]) == 0
    assert "hunter2" not in capsys.readouterr().out

    assert main(["-p", "live", "--json"]) == 0
    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert json.loads(out)[0]["source"] == "ibkr"


def test_restart_needs_a_source(capsys):
    assert main(["--restart"]) == 2
    assert "need -s/--source" in capsys.readouterr().err


def test_restart_prints_the_reply(monkeypatch, capsys):
    with FakeIBC(reply="OK Restarting") as ibc:
        monkeypatch.setenv("ANTLIA_IBKR_CONTROL_PORT", str(ibc.port))
        assert main(["-s", "ibkr", "-p", "live", "--restart"]) == 0
    assert capsys.readouterr().out.strip() == "OK Restarting"


def test_a_failed_restart_exits_nonzero_with_the_prose_on_stderr(monkeypatch, capsys):
    monkeypatch.setenv("ANTLIA_IBKR_CONTROL_PORT", str(dead_port()))
    assert main(["-s", "ibkr", "-p", "live", "--restart"]) == 1
    assert "cannot command the gateway" in capsys.readouterr().err
