"""Configuration loaded from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Europe/Bratislava"
DEFAULT_REQUEST_TIMEOUT = 20
DEFAULT_SESSION_TTL_MINUTES = 20
TRUTHY = ("1", "true", "yes", "on")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unusable."""


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    # Empty means "discover them from the account after logging in".
    subdomains: tuple[str, ...]
    timezone: ZoneInfo
    read_only: bool = False
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT
    session_ttl_minutes: int = DEFAULT_SESSION_TTL_MINUTES
    session_file: Path | None = None

    @classmethod
    def from_env(cls) -> "Config":
        username = os.environ.get("EDUPAGE_USERNAME", "").strip()
        password = os.environ.get("EDUPAGE_PASSWORD", "")

        missing = [
            name
            for name, value in (("EDUPAGE_USERNAME", username), ("EDUPAGE_PASSWORD", password))
            if not value
        ]
        if missing:
            raise ConfigError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                "Use the same username (usually your email) and password you sign in "
                "to EduPage with."
            )

        subdomains = tuple(
            _normalise_subdomain(s)
            for s in os.environ.get("EDUPAGE_SUBDOMAINS", "").replace(";", ",").split(",")
            if s.strip()
        )

        tz_name = os.environ.get("EDUPAGE_TIMEZONE", "").strip() or DEFAULT_TIMEZONE
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(
                f"EDUPAGE_TIMEZONE={tz_name!r} is not a valid IANA timezone name "
                "(e.g. 'Europe/Bratislava', 'Europe/Prague', 'UTC')."
            ) from exc

        session_file = os.environ.get("EDUPAGE_SESSION_FILE", "").strip()

        return cls(
            username=username,
            password=password,
            subdomains=subdomains,
            timezone=tz,
            read_only=os.environ.get("EDUPAGE_READ_ONLY", "").strip().lower() in TRUTHY,
            request_timeout=_positive_int("EDUPAGE_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT),
            session_ttl_minutes=_positive_int(
                "EDUPAGE_SESSION_TTL_MINUTES", DEFAULT_SESSION_TTL_MINUTES
            ),
            session_file=Path(session_file).expanduser() if session_file else None,
        )


def _normalise_subdomain(value: str) -> str:
    """Accept 'myschool', 'myschool.edupage.org' or 'https://myschool.edupage.org/'."""
    value = value.strip().lower()
    for prefix in ("https://", "http://"):
        value = value.removeprefix(prefix)
    value = value.split("/", 1)[0].removesuffix(".edupage.org")
    if not value or not all(c.isalnum() or c == "-" for c in value):
        raise ConfigError(
            f"EDUPAGE_SUBDOMAINS contains {value!r}, which is not an EduPage subdomain. "
            "Use the part before .edupage.org, e.g. 'myschool' for https://myschool.edupage.org."
        )
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a whole number.") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be greater than zero.")
    return value
