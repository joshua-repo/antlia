from __future__ import annotations

import pytest

from antlia.auth import credentials
from antlia.auth.errors import InvalidCredential, MissingCredential, UnknownProfile
from antlia.auth.spec import Field, SourceSpec

SPEC = SourceSpec(
    name="demo",
    fields=(
        Field("api_key", secret=True),
        Field("host", required=False, default="127.0.0.1"),
        Field("port", required=False, default=1234, cast=int),
        Field("readonly", required=False, default=True, cast=bool),
        Field("note", required=False),
    ),
    profiles=True,
    default_profile="live",
)


def test_env_beats_file(monkeypatch, config):
    config('[demo]\napi_key = "from-file"\n')
    monkeypatch.setenv("ANTLIA_DEMO_API_KEY", "from-env")
    assert credentials.resolve(SPEC)["api_key"] == "from-env"


def test_profile_env_beats_source_env(monkeypatch, config):
    monkeypatch.setenv("ANTLIA_DEMO_API_KEY", "source")
    monkeypatch.setenv("ANTLIA_DEMO_PAPER_API_KEY", "profile")
    config("[demo.paper]\n")
    assert credentials.resolve(SPEC, "paper")["api_key"] == "profile"


def test_profile_section_beats_source_section(config):
    config('[demo]\napi_key = "source"\n\n[demo.paper]\napi_key = "profile"\n')
    assert credentials.resolve(SPEC, "paper")["api_key"] == "profile"
    assert credentials.resolve(SPEC, "live")["api_key"] == "source"


def test_overrides_beat_everything(monkeypatch, config):
    config('[demo]\napi_key = "file"\n')
    monkeypatch.setenv("ANTLIA_DEMO_API_KEY", "env")
    assert credentials.resolve(SPEC, overrides={"api_key": "arg"})["api_key"] == "arg"


def test_defaults_fill_optional_fields(config):
    config('[demo]\napi_key = "k"\n')
    cred = credentials.resolve(SPEC)
    assert cred["host"] == "127.0.0.1"
    assert cred["port"] == 1234
    assert cred["note"] is None


def test_casts_apply_to_env_strings(monkeypatch, config):
    config('[demo]\napi_key = "k"\n')
    monkeypatch.setenv("ANTLIA_DEMO_PORT", "4321")
    monkeypatch.setenv("ANTLIA_DEMO_READONLY", "no")
    cred = credentials.resolve(SPEC)
    assert cred["port"] == 4321
    assert cred["readonly"] is False


def test_bad_cast_names_the_field_and_origin(monkeypatch, config):
    config('[demo]\napi_key = "k"\n')
    monkeypatch.setenv("ANTLIA_DEMO_PORT", "not-a-port")
    with pytest.raises(InvalidCredential) as exc:
        credentials.resolve(SPEC)
    assert "demo.port" in str(exc.value)
    assert "ANTLIA_DEMO_PORT" in str(exc.value)


def test_missing_required_lists_every_location_tried():
    with pytest.raises(MissingCredential) as exc:
        credentials.resolve(SPEC, "paper")
    message = str(exc.value)
    assert "$ANTLIA_DEMO_PAPER_API_KEY" in message
    assert "$ANTLIA_DEMO_API_KEY" in message
    assert "[demo.paper] api_key" in message
    assert "[demo] api_key" in message


def test_unknown_profile_rejected_when_others_are_configured(config):
    config('[demo]\napi_key = "k"\n\n[demo.paper]\n')
    with pytest.raises(UnknownProfile):
        credentials.resolve(SPEC, "typo")


def test_secrets_redact_in_repr(config):
    config('[demo]\napi_key = "hunter2"\n')
    cred = credentials.resolve(SPEC)
    assert "hunter2" not in repr(cred)
    assert cred.redacted()["api_key"] == "<set>"
    assert cred["api_key"] == "hunter2"


def test_loose_permissions_warn(config):
    config('[demo]\napi_key = "k"\n', mode=0o644)
    with pytest.warns(UserWarning, match="readable beyond its owner"):
        credentials.resolve(SPEC)


def test_missing_config_file_is_not_an_error(monkeypatch):
    monkeypatch.setenv("ANTLIA_DEMO_API_KEY", "k")
    assert credentials.load_config() == {}
    assert credentials.resolve(SPEC)["api_key"] == "k"


def test_profiles_lists_only_tables(config):
    config('[demo]\napi_key = "k"\n\n[demo.paper]\n\n[demo.live]\n')
    assert credentials.profiles("demo") == ["paper", "live"]
