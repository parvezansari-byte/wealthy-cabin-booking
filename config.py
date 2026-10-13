"""Application configuration, read from environment variables only.

Secrets (Supabase keys, SMTP password) never live in code or in the repository;
see .env.example for the full list. Values here are server-side only: Streamlit
renders on the server, so nothing in this module is ever sent to a browser.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:  # Optional: load a local .env during development.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - python-dotenv is a dev convenience
    pass


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(env: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}")
    return value


def parse_domains(raw: str | None) -> tuple[str, ...]:
    """'wealthy.in, Example.com' -> ('wealthy.in', 'example.com'); rejects '@' and blanks."""
    if not raw:
        return ()
    domains = []
    for part in raw.split(","):
        d = part.strip().lower()
        if not d:
            continue
        if "@" in d or "." not in d or " " in d:
            raise ConfigError(f"ALLOWED_EMAIL_DOMAIN contains an invalid domain: {part!r}")
        domains.append(d)
    return tuple(dict.fromkeys(domains))


@dataclass(frozen=True)
class SmtpConfig:
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = field(default="", repr=False)
    sender: str = ""
    starttls: bool = True
    use_ssl: bool = False
    timeout_seconds: int = 15

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender)


@dataclass(frozen=True)
class AppConfig:
    supabase_url: str
    supabase_anon_key: str = field(repr=False)
    supabase_service_role_key: str = field(default="", repr=False)
    allowed_email_domains: tuple[str, ...] = ("wealthy.in",)
    app_name: str = "Wealthy Office Cabin Booking"
    company_name: str = "Wealthy"
    logo_url: str = ""
    office_timezone: str = "Asia/Kolkata"
    notifications_enabled: bool = True
    smtp: SmtpConfig = field(default_factory=SmtpConfig)
    app_base_url: str = ""
    session_idle_minutes: int = 60
    environment: str = "production"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.office_timezone)

    @property
    def can_send_email(self) -> bool:
        return self.notifications_enabled and self.smtp.configured and bool(self.supabase_service_role_key)


def load_config(env: Mapping[str, str] | None = None) -> AppConfig:
    env = os.environ if env is None else env

    missing = [k for k in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "ALLOWED_EMAIL_DOMAIN") if not env.get(k)]
    if missing:
        raise ConfigError("Missing required environment variables: " + ", ".join(missing))

    url = env["SUPABASE_URL"].strip().rstrip("/")
    if not url.startswith("https://") and not url.startswith("http://localhost") \
            and not url.startswith("http://127.0.0.1"):
        raise ConfigError("SUPABASE_URL must use https://")

    domains = parse_domains(env.get("ALLOWED_EMAIL_DOMAIN"))
    if not domains:
        raise ConfigError("ALLOWED_EMAIL_DOMAIN must contain at least one domain, e.g. wealthy.in")

    tz_name = (env.get("OFFICE_TIMEZONE") or "Asia/Kolkata").strip()
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"OFFICE_TIMEZONE is not a valid IANA time zone: {tz_name}") from exc

    smtp = SmtpConfig(
        host=(env.get("SMTP_HOST") or "").strip(),
        port=_int(env, "SMTP_PORT", 587, 1, 65535),
        username=(env.get("SMTP_USERNAME") or "").strip(),
        password=env.get("SMTP_PASSWORD") or "",
        sender=(env.get("SMTP_FROM") or "").strip(),
        starttls=_bool(env.get("SMTP_STARTTLS"), True),
        use_ssl=_bool(env.get("SMTP_USE_SSL"), False),
        timeout_seconds=_int(env, "SMTP_TIMEOUT_SECONDS", 15, 1, 120),
    )

    return AppConfig(
        supabase_url=url,
        supabase_anon_key=env["SUPABASE_ANON_KEY"].strip(),
        supabase_service_role_key=(env.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip(),
        allowed_email_domains=domains,
        app_name=(env.get("APP_NAME") or "Wealthy Office Cabin Booking").strip(),
        company_name=(env.get("COMPANY_NAME") or "Wealthy").strip(),
        logo_url=(env.get("COMPANY_LOGO_URL") or "").strip(),
        office_timezone=tz_name,
        notifications_enabled=_bool(env.get("NOTIFICATIONS_ENABLED"), True),
        smtp=smtp,
        app_base_url=(env.get("APP_BASE_URL") or "").strip().rstrip("/"),
        session_idle_minutes=_int(env, "SESSION_IDLE_MINUTES", 60, 5, 24 * 60),
        environment=(env.get("APP_ENV") or "production").strip().lower(),
    )


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return load_config()
