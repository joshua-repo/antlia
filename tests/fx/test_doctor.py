"""`python -m antlia.fx` -- the admin surface, not a product one."""

from __future__ import annotations

import pytest

from antlia import fx
from antlia.auth.errors import ConnectionFailed
from antlia.fx.__main__ import main
from tests.fx.fakes import FakeRates

MARKET = {"USD": 1.0, "GBP": 0.73869, "JPY": 160.038, "HKD": 7.8391}


@pytest.fixture(autouse=True)
def chain():
    for name in fx.chain():
        fx.unregister(name)
    fx.register("primary", FakeRates("primary", MARKET))


def test_it_prints_both_orientations(capsys):
    assert main(["-c", "USD,GBP"]) == 0
    out = capsys.readouterr().out
    assert "0.738690" in out  # per 1 USD
    assert "1.353748" in out  # per 1 GBP -- what a broker reports
    assert "broker's own exchange rate" in out


def test_convert_reports_the_figure(capsys):
    assert main(["--convert", "83289.64", "GBP", "USD"]) == 0
    assert "112,753.17 USD" in capsys.readouterr().out


def test_convert_pulls_in_the_currencies_it_needs(capsys):
    # Neither currency is in the default set the flag would otherwise ask for.
    fx.register("primary", FakeRates("primary", MARKET | {"CHF": 0.80}))
    assert main(["-c", "USD", "--convert", "100", "CHF", "GBP"]) == 0
    assert "GBP" in capsys.readouterr().out


def test_an_unconvertible_pair_fails_loudly(capsys):
    assert main(["-c", "USD,GBP", "--convert", "100", "CHF", "USD"]) == 1
    assert "no rate" in capsys.readouterr().err


def test_json_is_machine_readable(capsys):
    import json

    assert main(["-c", "USD,GBP", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["base"] == "USD"
    assert payload["rates"]["GBP"] == 0.73869
    assert payload["stale"] is False


def test_verify_walks_every_source_not_just_the_first(capsys):
    fx.register("fallback", FakeRates("fallback", MARKET))
    assert main(["-c", "USD,GBP", "--verify"]) == 0
    out = capsys.readouterr().out
    # A dead fallback has to be found before the primary needs it, so verify
    # is the one mode that does not stop at the first source that works.
    assert "ok primary" in out and "ok fallback" in out
    assert "primary -> fallback" in out


def test_verify_reports_a_broken_source_and_exits_nonzero(capsys):
    fx.register("fallback", FakeRates("fallback", fail=ConnectionFailed("fallback", "403")))
    assert main(["-c", "USD,GBP", "--verify"]) == 1
    out = capsys.readouterr().out
    assert "ok primary" in out and "-- fallback" in out


def test_a_total_failure_is_reported_on_stderr(capsys):
    fx.unregister("primary")
    assert main(["-c", "USD,GBP"]) == 1
    assert "FAILED" in capsys.readouterr().err


def test_stale_is_announced(capsys):
    main(["-c", "USD,GBP"])  # populates the cache
    fx.register("primary", FakeRates("primary", fail=ConnectionFailed("primary", "down")))
    assert main(["-c", "USD,GBP", "--fresh"]) == 0
    assert "STALE" in capsys.readouterr().out


def test_clear_cache_removes_the_file(capsys):
    from antlia.fx import cache

    main(["-c", "USD,GBP"])
    assert cache.path().exists()
    assert main(["--clear-cache"]) == 0
    assert not cache.path().exists()
    assert "removed" in capsys.readouterr().out


def test_currencies_are_upper_cased(capsys):
    assert main(["-c", "usd,gbp"]) == 0
    assert "GBP" in capsys.readouterr().out
