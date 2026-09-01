"""`antlia-history`: the commands a metered key gets debugged with."""

from __future__ import annotations

import argparse
import datetime as dt

import pytest

from antlia import history
from antlia.history.cli import main

from .fakes import FakeHistory

D = dt.date


@pytest.fixture
def fake(tmp_path):
    src = FakeHistory()
    history.register("fake", src, first=True)
    return src


def run(capsys, *argv):
    code = main(list(argv))
    return code, capsys.readouterr().out


def test_the_doctor_reports_an_empty_store(capsys, tmp_path):
    code, out = run(capsys, "--store", str(tmp_path))
    assert code == 0
    assert "nothing has been ingested yet" in out


def test_plan_prints_the_cost_without_paying_it(capsys, fake, tmp_path):
    code, out = run(
        capsys,
        "--store",
        str(tmp_path),
        "plan",
        "equity_eod",
        "AAPL",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-16",
    )
    assert code == 0
    assert "2 requests" in out
    assert fake.calls == []


def test_fill_then_doctor_shows_what_is_held(capsys, fake, tmp_path):
    run(
        capsys,
        "--store",
        str(tmp_path),
        "fill",
        "equity_eod",
        "AAPL",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-16",
    )
    code, out = run(capsys, "--store", str(tmp_path))
    assert code == 0
    assert "fake:equity_eod" in out and "AAPL" in out


def test_fill_respects_a_request_limit(capsys, fake, tmp_path):
    code, out = run(
        capsys,
        "--store",
        str(tmp_path),
        "fill",
        "equity_eod",
        "AAPL",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-31",
        "--limit",
        "1",
    )
    assert code == 0
    assert "1 requests" in out


def test_coverage_reports_the_gaps(capsys, fake, tmp_path):
    run(
        capsys,
        "--store",
        str(tmp_path),
        "fill",
        "equity_eod",
        "AAPL",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-09",
    )
    code, out = run(
        capsys,
        "--store",
        str(tmp_path),
        "coverage",
        "equity_eod",
        "AAPL",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-16",
    )
    assert code == 0
    assert "missing 2026-01-10 .. 2026-01-16" in out


def test_read_prints_stored_rows_without_fetching(capsys, fake, tmp_path):
    run(
        capsys,
        "--store",
        str(tmp_path),
        "fill",
        "equity_eod",
        "AAPL",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-09",
    )
    fake.calls.clear()
    code, out = run(
        capsys,
        "--store",
        str(tmp_path),
        "read",
        "equity_eod",
        "AAPL",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-09",
    )
    assert code == 0 and "5 rows" in out
    assert fake.calls == []


def test_verify_reports_each_source(capsys, fake, tmp_path):
    code, out = run(capsys, "--store", str(tmp_path), "--source", "fake", "verify")
    assert code == 0 and "ok fake" in out


def test_verify_fails_loudly(capsys, tmp_path):
    class Broken(FakeHistory):
        def verify(self):
            raise history.IngestFailed("fake", "the data channel is down")

    history.register("fake", Broken(), first=True)
    code, out = run(capsys, "--store", str(tmp_path), "--source", "fake", "verify")
    assert code == 1 and "FAILED fake" in out


def test_horizon_says_so_when_a_source_declares_one(capsys, tmp_path):
    history.register("fake", FakeHistory(earliest=D(2024, 1, 1)), first=True)
    code, out = run(capsys, "--store", str(tmp_path), "--source", "fake", "horizon")
    assert code == 0 and "2024-01-01" in out


def test_check_reports_a_clean_store(capsys, fake, tmp_path):
    run(
        capsys,
        "--store",
        str(tmp_path),
        "fill",
        "equity_eod",
        "AAPL",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-09",
    )
    code, out = run(capsys, "--store", str(tmp_path), "check")
    assert code == 0 and "nothing wrong" in out


def test_check_finds_and_repairs_a_bad_file(capsys, fake, tmp_path):
    run(
        capsys,
        "--store",
        str(tmp_path),
        "fill",
        "equity_eod",
        "AAPL",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-09",
    )
    victim = next((tmp_path / "raw" / "fake" / "equity_eod").glob("**/*.parquet"))
    victim.write_bytes(b"truncated")

    code, out = run(capsys, "--store", str(tmp_path), "check")
    assert code == 1 and "unreadable" in out
    code, out = run(capsys, "--store", str(tmp_path), "check", "--repair")
    assert "repaired" in out
    assert not victim.exists()


def test_fill_refresh_reruns_a_covered_window(capsys, fake, tmp_path):
    run(
        capsys,
        "--store",
        str(tmp_path),
        "fill",
        "equity_eod",
        "AAPL",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-09",
    )
    code, out = run(
        capsys,
        "--store",
        str(tmp_path),
        "fill",
        "equity_eod",
        "AAPL",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-09",
    )
    assert "0 requests" in out
    code, out = run(
        capsys,
        "--store",
        str(tmp_path),
        "fill",
        "equity_eod",
        "AAPL",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-09",
        "--refresh",
    )
    assert "1 requests" in out


def test_fill_takes_several_symbols(capsys, fake, tmp_path):
    code, out = run(
        capsys,
        "--store",
        str(tmp_path),
        "fill",
        "equity_eod",
        "AAPL",
        "MSFT",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-09",
    )
    assert code == 0
    assert "AAPL equity_eod" in out and "MSFT equity_eod" in out


def test_read_takes_several_symbols_in_one_frame(capsys, fake, tmp_path):
    run(
        capsys,
        "--store",
        str(tmp_path),
        "fill",
        "equity_eod",
        "AAPL",
        "MSFT",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-09",
    )
    code, out = run(
        capsys,
        "--store",
        str(tmp_path),
        "read",
        "equity_eod",
        "AAPL",
        "MSFT",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-09",
    )
    assert code == 0 and "10 rows" in out


def test_ingests_lists_the_moments_as_of_accepts(capsys, fake, tmp_path):
    run(
        capsys,
        "--store",
        str(tmp_path),
        "fill",
        "equity_eod",
        "AAPL",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-09",
    )
    code, out = run(capsys, "--store", str(tmp_path), "ingests", "equity_eod", "AAPL")
    assert code == 0 and "1 writes" in out


def test_read_honours_as_of(capsys, fake, tmp_path):
    run(
        capsys,
        "--store",
        str(tmp_path),
        "fill",
        "equity_eod",
        "AAPL",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-09",
    )
    stamps = history.ingests("equity_eod", "AAPL", store=tmp_path)
    code, out = run(
        capsys,
        "--store",
        str(tmp_path),
        "read",
        "equity_eod",
        "AAPL",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-09",
        "--as-of",
        stamps[0].isoformat(),
    )
    assert code == 0 and "5 rows" in out


def test_every_command_is_reachable_and_declares_its_flags():
    # The table is the CLI. A command added without a handler, or a flag group
    # naming an argument its handler never reads, shows up here rather than as
    # a traceback the first time someone runs it.
    from antlia.history import cli

    names = [c.name for c in cli.COMMANDS]
    # Order is presentation, not alphabetical -- it is what `--help` prints,
    # and doctor-before-verify-before-the-rest is deliberate. Only uniqueness
    # is a rule.
    assert len(names) == len(set(names)), "duplicate command names"
    for command in cli.COMMANDS:
        assert callable(command.run)
        parser = argparse.ArgumentParser()
        for group in command.groups:
            group(parser)


def test_the_default_command_is_the_doctor(capsys, tmp_path):
    from antlia.history import cli

    code, out = run(capsys, "--store", str(tmp_path))
    assert code == 0 and "store" in out
    assert cli.COMMANDS[0].name == "doctor"


def test_read_passes_the_shape_flags_through(capsys, fake, tmp_path):
    # Reading never fetches, so an option window filled at one --max-dte must
    # read back when asked the same way. Without the pass-through the plan
    # considers every expiration and refuses while the rows are sitting there.
    run(
        capsys,
        "--store",
        str(tmp_path),
        "fill",
        "option_eod",
        "AAPL",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-09",
        "--max-dte",
        "20",
    )
    code, out = run(
        capsys,
        "--store",
        str(tmp_path),
        "read",
        "option_eod",
        "AAPL",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-09",
        "--max-dte",
        "20",
    )
    assert code == 0 and "rows" in out
