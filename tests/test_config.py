from __future__ import annotations

from pathlib import Path

import pytest

from edupage_mcp.config import Config, ConfigError

ENV = (
    "EDUPAGE_USERNAME", "EDUPAGE_PASSWORD", "EDUPAGE_SUBDOMAINS", "EDUPAGE_TIMEZONE",
    "EDUPAGE_READ_ONLY", "EDUPAGE_REQUEST_TIMEOUT", "EDUPAGE_SESSION_TTL_MINUTES",
    "EDUPAGE_SESSION_FILE",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EDUPAGE_USERNAME", "parent@example.com")
    monkeypatch.setenv("EDUPAGE_PASSWORD", "secret")


def test_defaults():
    config = Config.from_env()
    assert config.subdomains == ()
    assert str(config.timezone) == "Europe/Bratislava"
    assert config.read_only is False
    assert config.request_timeout == 20
    assert config.session_file is None


def test_missing_credentials_name_the_variables(monkeypatch):
    monkeypatch.delenv("EDUPAGE_PASSWORD")
    with pytest.raises(ConfigError, match="EDUPAGE_PASSWORD"):
        Config.from_env()


@pytest.mark.parametrize(
    "raw",
    ["school-a, https://School-B.edupage.org/", "school-a;school-b.edupage.org"],
)
def test_subdomains_are_normalised(monkeypatch, raw):
    monkeypatch.setenv("EDUPAGE_SUBDOMAINS", raw)
    assert Config.from_env().subdomains == ("school-a", "school-b")


def test_a_bad_subdomain_is_rejected(monkeypatch):
    monkeypatch.setenv("EDUPAGE_SUBDOMAINS", "not a school")
    with pytest.raises(ConfigError, match="not an EduPage subdomain"):
        Config.from_env()


def test_options(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("EDUPAGE_TIMEZONE", "Europe/Prague")
    monkeypatch.setenv("EDUPAGE_READ_ONLY", "yes")
    monkeypatch.setenv("EDUPAGE_REQUEST_TIMEOUT", "45")
    monkeypatch.setenv("EDUPAGE_SESSION_FILE", str(tmp_path / "s.json"))
    config = Config.from_env()
    assert str(config.timezone) == "Europe/Prague"
    assert config.read_only is True
    assert config.request_timeout == 45
    assert config.session_file == tmp_path / "s.json"


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("EDUPAGE_TIMEZONE", "Mars/Olympus", "IANA"),
        ("EDUPAGE_REQUEST_TIMEOUT", "soon", "whole number"),
        ("EDUPAGE_SESSION_TTL_MINUTES", "0", "greater than zero"),
    ],
)
def test_bad_values_are_reported(monkeypatch, name, value, message):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=message):
        Config.from_env()
