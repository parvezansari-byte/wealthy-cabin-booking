"""Small, dependency-free security helpers (all unit tested)."""
from __future__ import annotations

import html
import re
from typing import Iterable

_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def email_domain(email: str | None) -> str:
    email = normalize_email(email)
    return email.rsplit("@", 1)[1] if "@" in email else ""


def is_company_email(email: str | None, allowed_domains: Iterable[str]) -> bool:
    """Exact domain match. 'a@sub.wealthy.in', 'a@evilwealthy.in' and
    'a@wealthy.in.example.com' are all rejected for allowed domain 'wealthy.in'."""
    email = normalize_email(email)
    if len(email) > 254 or not _EMAIL_RE.match(email):
        return False
    return email_domain(email) in {d.strip().lower() for d in allowed_domains}


def password_problems(password: str) -> list[str]:
    """Server-side password policy (Supabase enforces its own minimum as well)."""
    problems = []
    if len(password) < 10:
        problems.append("at least 10 characters")
    if len(password) > 72:
        problems.append("at most 72 characters")
    if not re.search(r"[A-Za-z]", password):
        problems.append("a letter")
    if not re.search(r"\d", password):
        problems.append("a number")
    return problems


def esc(value: object) -> str:
    """HTML-escape anything that is interpolated into custom HTML (cards, badges)."""
    return html.escape("" if value is None else str(value), quote=True)


def csv_safe(value: object) -> object:
    """Neutralise spreadsheet formula injection in exported CSV cells."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def clean_text(value: str | None, max_len: int) -> str:
    """Trim, drop control characters, and cap length for free-text inputs."""
    value = (value or "").strip()
    value = "".join(ch for ch in value if ch in "\n\t" or ord(ch) >= 32)
    return value[:max_len]
