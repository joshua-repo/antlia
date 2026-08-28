"""Layered credential resolution: environment first, then the config file.

    ANTLIA_<SOURCE>_<PROFILE>_<FIELD>     environment, profile-specific
    ANTLIA_<SOURCE>_<FIELD>               environment, source-wide
    [<source>.<profile>] <field>          ~/.antlia/credentials.toml
    [<source>] <field>                    ~/.antlia/credentials.toml
    spec default

Environment wins so a shell or a CI job can override a checked-in habit without
editing anything. The file exists because profiles (paper vs live, two accounts
at one broker) turn into an unreadable pile of environment variables fast.

Deliberately not a keyring: this runs on WSL2, where there is no Secret Service
daemon by default and `keyring` either fails or silently falls back to a
plaintext backend -- which is the file below, with extra steps and less clarity
about what is actually protecting the secret. What protects it here is file
permissions, and `check_permissions()` says so out loud when they are wrong.
"""

from __future__ import annotations

import os
import stat
import tomllib
import warnings
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from antlia.auth.errors import InvalidCredential, MissingCredential, UnknownProfile
from antlia.auth.spec import SourceSpec

CONFIG_FILENAME = "credentials.toml"


def home() -> Path:
    """The antlia config directory: `$ANTLIA_HOME`, else `~/.antlia`."""
    override = os.environ.get("ANTLIA_HOME")
    return Path(override).expanduser() if override else Path.home() / ".antlia"


def config_path() -> Path:
    return home() / CONFIG_FILENAME


def _env_key(*parts: str) -> str:
    joined = "_".join(parts)
    return "ANTLIA_" + "".join(c if c.isalnum() else "_" for c in joined).upper()


def check_permissions(path: Path | None = None) -> str | None:
    """Return a warning string if the config file is readable by others.

    Returns None when the file is absent or already tight. Called on every load;
    exposed so a `doctor` command can report it without triggering a load.
    """
    path = path or config_path()
    try:
        mode = path.stat().st_mode
    except OSError:
        return None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return (
            f"{path} is readable beyond its owner (mode {stat.S_IMODE(mode):04o}); "
            f"run: chmod 600 {path}"
        )
    return None


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Parse the credentials file. A missing file is empty config, not an error."""
    path = path or config_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise InvalidCredential("<config>", str(path), path, str(exc)) from exc

    complaint = check_permissions(path)
    if complaint:
        warnings.warn(complaint, stacklevel=2)

    return tomllib.loads(raw.decode("utf-8"))


def lookup(section: Mapping[str, Any], spellings: Iterable[str]) -> Any:
    """Find a field in a config section, case-insensitively, by any spelling.

    TOML keys here are hand-typed by the user, copying names off a vendor's
    settings page. Case is not carrying meaning, and `SECRET_KEY` failing to
    match `api_secret` produces "no value for it" about a value that is plainly
    there -- the exact confusion this module exists to prevent.
    """
    folded = {str(k).casefold(): v for k, v in section.items()}
    for spelling in spellings:
        found = folded.get(spelling.casefold())
        if found is not None:
            return found
    return None


def _section(config: Mapping[str, Any], source: str) -> dict[str, Any]:
    """Source-level scalars only -- nested tables are profiles, not values."""
    raw = config.get(source)
    if not isinstance(raw, Mapping):
        return {}
    return {k: v for k, v in raw.items() if not isinstance(v, Mapping)}


def profiles(source: str, config: Mapping[str, Any] | None = None) -> list[str]:
    """Profile names configured for a source, in file order."""
    conf = load_config() if config is None else config
    raw = conf.get(source)
    if not isinstance(raw, Mapping):
        return []
    return [k for k, v in raw.items() if isinstance(v, Mapping)]


def _profile_section(config: Mapping[str, Any], source: str, profile: str | None) -> dict[str, Any]:
    if profile is None:
        return {}
    raw = config.get(source)
    if not isinstance(raw, Mapping):
        return {}
    section = raw.get(profile)
    if section is None:
        return {}
    if not isinstance(section, Mapping):
        raise InvalidCredential(source, profile, section, "expected a [table], got a scalar")
    return dict(section)


@dataclass(frozen=True, slots=True)
class Credential:
    """Resolved settings for one source/profile. Secrets redact in repr."""

    source: str
    profile: str | None
    values: Mapping[str, Any]
    secrets: frozenset[str] = frozenset()

    def __getitem__(self, name: str) -> Any:
        return self.values[name]

    def get(self, name: str, default: Any = None) -> Any:
        return self.values.get(name, default)

    def redacted(self) -> dict[str, Any]:
        return {
            k: ("<set>" if k in self.secrets and v is not None else v)
            for k, v in self.values.items()
        }

    def __repr__(self) -> str:
        label = self.source if self.profile is None else f"{self.source}:{self.profile}"
        return f"Credential({label}, {self.redacted()})"


def resolve(
    spec: SourceSpec,
    profile: str | None = None,
    overrides: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> Credential:
    """Resolve every field of `spec`, layer by layer.

    `overrides` sit above everything -- they are the caller passing an explicit
    argument, which must beat any ambient configuration.

    Raises `MissingCredential` naming every location consulted. It resolves the
    whole spec before deciding, so nothing here reaches the network: a cache-hit
    path that never calls `session()` never authenticates.
    """
    conf = load_config() if config is None else config
    if spec.profiles and profile is None:
        profile = spec.default_profile
    if profile is not None and spec.profiles:
        known = profiles(spec.name, conf)
        if known and profile not in known and spec.default_profile != profile:
            raise UnknownProfile(spec.name, profile, known)

    file_source = _section(conf, spec.name)
    file_profile = _profile_section(conf, spec.name, profile)
    overrides = overrides or {}

    values: dict[str, Any] = {}
    secrets: set[str] = set()

    for f in spec.fields:
        if f.secret:
            secrets.add(f.name)

        tried: list[str] = []
        found: object | None = None
        origin = ""

        if f.name in overrides and overrides[f.name] is not None:
            found, origin = overrides[f.name], "call argument"
        else:
            candidates: list[tuple[str, object | None]] = []
            spellings = f.spellings()
            for spelling in spellings:
                if profile is not None:
                    key = _env_key(spec.name, profile, spelling)
                    tried.append(f"${key}")
                    candidates.append((f"${key}", os.environ.get(key)))
                key = _env_key(spec.name, spelling)
                tried.append(f"${key}")
                candidates.append((f"${key}", os.environ.get(key)))
            if profile is not None:
                tried.append(f"[{spec.name}.{profile}] {f.name} in {config_path()}")
                candidates.append((tried[-1], lookup(file_profile, spellings)))
            tried.append(f"[{spec.name}] {f.name} in {config_path()}")
            candidates.append((tried[-1], lookup(file_source, spellings)))

            for where, candidate in candidates:
                if candidate is not None:
                    found, origin = candidate, where
                    break

        if found is None:
            if f.default is not None:
                values[f.name] = f.default
                continue
            if f.required:
                raise MissingCredential(spec.name, f.name, tried)
            values[f.name] = None
            continue

        try:
            values[f.name] = f.parse(found)
        except (TypeError, ValueError) as exc:
            raise InvalidCredential(spec.name, f.name, found, f"{exc} (from {origin})") from exc

    for name, value in overrides.items():
        if name not in values:
            values[name] = value

    return Credential(spec.name, profile, values, frozenset(secrets))
