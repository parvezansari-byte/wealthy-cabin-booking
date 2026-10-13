import pytest

from config import ConfigError, load_config, parse_domains
from core.errors import AppError, from_auth, from_postgrest
from core.ratelimit import RateLimiter
from core.security import clean_text, csv_safe, esc, is_company_email, password_problems

DOMAINS = ("wealthy.in",)

BASE_ENV = {"SUPABASE_URL": "https://abc.supabase.co", "SUPABASE_ANON_KEY": "anon",
            "ALLOWED_EMAIL_DOMAIN": "wealthy.in"}


@pytest.mark.parametrize("email", ["parvez.ansari@wealthy.in", "  First.Last@WEALTHY.IN ", "a+tag@wealthy.in"])
def test_company_emails_accepted(email):
    assert is_company_email(email, DOMAINS)


@pytest.mark.parametrize("email", [
    "me@gmail.com", "me@yahoo.com", "me@outlook.com", "me@hotmail.com",
    "me@evilwealthy.in", "me@wealthy.in.evil.com", "me@sub.wealthy.in", "me@wealthy.co",
    "wealthy.in", "@wealthy.in", "me@@wealthy.in", "me@wealthy.in@gmail.com", "", None,
    "me <script>@wealthy.in",
])
def test_other_emails_rejected(email):
    assert not is_company_email(email, DOMAINS)


def test_password_policy():
    assert password_problems("short1") == ["at least 10 characters"]
    assert "a number" in password_problems("onlyletterspassword")
    assert password_problems("Str0ng-enough-pass") == []


def test_escape_and_csv_safety():
    assert esc('<img src=x onerror="alert(1)">') == "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;"
    assert csv_safe("=HYPERLINK(\"http://x\")") == "'=HYPERLINK(\"http://x\")"
    assert csv_safe("+1") == "'+1" and csv_safe("@SUM(A1)") == "'@SUM(A1)"
    assert csv_safe("Team sync") == "Team sync" and csv_safe(5) == 5


def test_clean_text_strips_control_chars_and_caps():
    assert clean_text("  hi\x00there\x07  ", 100) == "hithere"
    assert clean_text("x" * 300, 120) == "x" * 120


def test_config_requires_domain_and_https():
    with pytest.raises(ConfigError):
        load_config({**BASE_ENV, "ALLOWED_EMAIL_DOMAIN": ""})
    with pytest.raises(ConfigError):
        load_config({**BASE_ENV, "SUPABASE_URL": "http://abc.supabase.co"})
    with pytest.raises(ConfigError):
        load_config({**BASE_ENV, "OFFICE_TIMEZONE": "Mars/Base"})
    cfg = load_config(BASE_ENV)
    assert cfg.allowed_email_domains == ("wealthy.in",)
    assert cfg.app_name == "Wealthy Office Cabin Booking"
    assert cfg.office_timezone == "Asia/Kolkata"
    assert not cfg.can_send_email  # no SMTP / service key configured


def test_secrets_are_not_in_repr():
    cfg = load_config({**BASE_ENV, "SUPABASE_SERVICE_ROLE_KEY": "super-secret", "SMTP_PASSWORD": "pw-secret",
                       "SMTP_HOST": "smtp.example.com", "SMTP_FROM": "noreply@wealthy.in"})
    assert "super-secret" not in repr(cfg) and "pw-secret" not in repr(cfg)


def test_parse_domains():
    assert parse_domains(" Wealthy.in , wealthy.in") == ("wealthy.in",)
    with pytest.raises(ConfigError):
        parse_domains("@wealthy.in")


class _ApiErr(Exception):
    def __init__(self, code, message, details=None):
        self.code, self.message, self.details = code, message, details


def test_postgrest_errors_are_mapped_without_leaking_internals():
    err = from_postgrest(_ApiErr("P0001", "CABIN_ALREADY_BOOKED", "Cabin 01 is already booked …"))
    assert err.code == "CABIN_ALREADY_BOOKED" and "already booked" in err.message
    raw = from_postgrest(_ApiErr("42P01", 'relation "public.secret" does not exist'))
    assert "relation" not in raw.message and raw.code == "UNEXPECTED"
    assert from_postgrest(_ApiErr("23P01", "conflicting key value violates exclusion constraint")).code \
        == "CABIN_ALREADY_BOOKED"
    assert from_postgrest(_ApiErr("PGRST301", "JWT expired")).code == "AUTH_REQUIRED"


class _AuthErr(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.message, self.code = message, code


def test_auth_errors_are_mapped():
    assert from_auth(_AuthErr("Invalid login credentials", "invalid_credentials")).code == "invalid_credentials"
    assert from_auth(_AuthErr("Email not confirmed")).code == "email_not_confirmed"
    assert from_auth(_AuthErr("Database error saving new user")).code == "EMAIL_DOMAIN_NOT_ALLOWED"
    assert isinstance(from_auth(_AuthErr("weird")), AppError)


def test_rate_limiter_window():
    t = [0.0]
    rl = RateLimiter(3, 60, clock=lambda: t[0])
    assert all(rl.allow("k") for _ in range(3))
    assert not rl.allow("k") and rl.retry_after("k") > 0
    assert rl.allow("other")
    t[0] = 61
    assert rl.allow("k")


def test_streamlit_cloud_secrets_are_used_when_env_is_empty(monkeypatch):
    import streamlit

    import config

    for key in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "ALLOWED_EMAIL_DOMAIN", "NOTIFICATIONS_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(streamlit, "secrets", {
        "SUPABASE_URL": "https://abc.supabase.co", "SUPABASE_ANON_KEY": "anon",
        "ALLOWED_EMAIL_DOMAIN": "wealthy.in", "NOTIFICATIONS_ENABLED": False, "nested": {"x": 1}})
    config.get_config.cache_clear()
    try:
        cfg = config.get_config()
    finally:
        config.get_config.cache_clear()
    assert cfg.supabase_url == "https://abc.supabase.co"
    assert cfg.allowed_email_domains == ("wealthy.in",)
    assert cfg.notifications_enabled is False
