"""`python -m antlia.auth` -- the thing that gets run when something is wrong.

Its output is the product here, so the tests assert on what it prints.
"""

from __future__ import annotations

import stat

import pytest

from antlia import auth
from antlia.auth import __main__ as doctor
from antlia.auth.base import Provider
from antlia.auth.errors import ConnectionFailed
from antlia.auth.spec import Field, SourceSpec


class Fake(Provider):
    spec = SourceSpec(name="fake", fields=(Field("api_key", secret=True),), rate=3.0)

    def __init__(self, verify_result="round-trip ok", fail=False):
        self.verify_result = verify_result
        self.fail = fail
        self.verified = 0

    def connect(self, cred, limiter):
        return object()

    def verify(self, handle):
        self.verified += 1
        if self.fail:
            raise ConnectionFailed("fake", "401 -- key rejected")
        return self.verify_result


@pytest.fixture
def only_fake(config):
    config('[fake]\napi_key = "k"\n')
    provider = Fake()
    auth.register("fake", provider)
    for name in auth.registry.sources():
        if name != "fake":
            auth.unregister(name)
    return provider


def test_reports_resolved_sources_without_connecting(only_fake, capsys):
    assert doctor.main([]) == 0
    out = capsys.readouterr().out
    assert "ok fake" in out
    assert only_fake.verified == 0


def test_secrets_are_redacted_in_the_report(config, capsys):
    config('[fake]\napi_key = "hunter2"\n')
    auth.register("fake", Fake())
    doctor.main(["fake"])
    assert "hunter2" not in capsys.readouterr().out


def test_missing_credential_is_a_nonzero_exit(config, capsys):
    config("")
    auth.register("fake", Fake())
    assert doctor.main(["fake"]) == 1
    assert "no value for fake.api_key" in capsys.readouterr().out


def test_verify_runs_the_round_trip_and_prints_it(only_fake, capsys):
    assert doctor.main(["fake", "--verify"]) == 0
    assert "verify: round-trip ok" in capsys.readouterr().out
    assert only_fake.verified == 1


def test_a_failing_verify_is_a_nonzero_exit(config, capsys):
    config('[fake]\napi_key = "k"\n')
    auth.register("fake", Fake(fail=True))
    assert doctor.main(["fake", "--verify"]) == 1
    assert "401 -- key rejected" in capsys.readouterr().out


def test_connect_without_verify_makes_no_round_trip(only_fake, capsys):
    assert doctor.main(["fake", "--connect"]) == 0
    assert "connect:" in capsys.readouterr().out
    assert only_fake.verified == 0


def test_profile_flag_narrows_to_one(config, capsys):
    config("[ibkr.paper]\nport = 7497\n\n[ibkr.live]\nport = 7496\n")
    doctor.main(["ibkr", "-p", "live"])
    out = capsys.readouterr().out
    assert "ibkr:live" in out
    assert "ibkr:paper" not in out


def test_missing_sdk_is_reported_not_a_crash(config, capsys, monkeypatch):
    # A source whose SDK is absent must be skipped with a readable line, not
    # blow up the whole report for the sources that are fine.
    config('[trading212.demo]\napi_key = "k"\n')
    monkeypatch.setattr(doctor, "_installed", lambda package: False)
    assert doctor.main(["trading212", "--verify"]) == 0
    assert "SDK not installed" in capsys.readouterr().out


def test_init_writes_an_owner_only_file(capsys):
    assert doctor.main(["--init"]) == 0
    path = auth.config_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert auth.check_permissions(path) is None
    assert "[ibkr.paper]" in path.read_text()


def test_init_refuses_to_clobber_without_force(config, capsys):
    config('[fake]\napi_key = "keep-me"\n')
    assert doctor.main(["--init"]) == 1
    assert "keep-me" in auth.config_path().read_text()
    assert doctor.main(["--init", "--force"]) == 0
    assert "keep-me" not in auth.config_path().read_text()


def test_the_template_it_writes_parses_and_resolves(capsys):
    doctor.main(["--init"])
    capsys.readouterr()
    assert doctor.main(["ibkr"]) == 0
    out = capsys.readouterr().out
    assert "'port': 7497" in out
    assert "'port': 7496" in out
