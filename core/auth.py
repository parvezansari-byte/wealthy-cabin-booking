"""Authentication use cases on top of Supabase Auth (GoTrue).

Identity is proven by Supabase Auth (bcrypt-hashed passwords, e-mailed one-time
codes). Authorisation is decided by the database (get_my_access, RLS). This
module adds server-side domain checks, password policy and rate limiting, and
never stores a password itself.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from core.errors import AppError, from_auth
from core.ratelimit import RateLimiter
from core.security import is_company_email, normalize_email, password_problems

log = logging.getLogger(__name__)

GENERIC_SIGNUP = ("If this address can register, we've e-mailed it a 6-digit verification code. "
                  "Enter it on the 'Verify e-mail' tab.")
GENERIC_RESET = ("If an account exists for this address, we've e-mailed it a 6-digit reset code. "
                 "Enter it below with your new password.")


@dataclass(frozen=True)
class Access:
    user_id: str
    email: str
    full_name: str
    department: str | None
    role: str
    status: str
    email_verified: bool
    domain_allowed: bool
    is_employee: bool
    is_admin: bool

    @classmethod
    def from_dict(cls, d: dict) -> "Access":
        return cls(
            user_id=str(d["user_id"]), email=d["email"], full_name=d.get("full_name") or "",
            department=d.get("department"), role=d.get("role", "employee"), status=d.get("status", "pending"),
            email_verified=bool(d.get("email_verified")), domain_allowed=bool(d.get("domain_allowed")),
            is_employee=bool(d.get("is_employee")), is_admin=bool(d.get("is_admin")))

    @property
    def display_name(self) -> str:
        return self.full_name or self.email.split("@", 1)[0]


@dataclass
class Limiters:
    login: RateLimiter
    signup: RateLimiter
    otp: RateLimiter
    reset: RateLimiter

    @classmethod
    def default(cls) -> "Limiters":
        return cls(login=RateLimiter(5, 15 * 60), signup=RateLimiter(3, 60 * 60),
                   otp=RateLimiter(10, 15 * 60), reset=RateLimiter(3, 60 * 60))


def evaluate_access(access: Access | None, allowed_domains: Iterable[str]) -> tuple[bool, str]:
    """Final gate used by the UI on every page load. The database is still the
    enforcement point; this just decides what screen to show."""
    if access is None:
        return False, "We couldn't find an employee profile for this account."
    if not is_company_email(access.email, allowed_domains) or not access.domain_allowed:
        return False, "Only official company e-mail addresses can use this system."
    if not access.email_verified:
        return False, "Please verify your e-mail address to continue."
    if access.status == "disabled":
        return False, "Your access has been disabled. Please contact an administrator."
    if access.status == "pending":
        return False, ("Your e-mail is verified. An administrator needs to approve your access "
                       "before you can book cabins.")
    if not access.is_employee:
        return False, "Your account is not authorised to use this system."
    return True, ""


class AuthService:
    def __init__(self, client, allowed_domains: Iterable[str], limiters: Limiters,
                 redirect_url: str = ""):
        self.client = client
        self.domains = tuple(allowed_domains)
        self.limiters = limiters
        self.redirect_url = redirect_url

    # ------------------------------------------------------------------ helpers
    def _require_company_email(self, email: str) -> str:
        email = normalize_email(email)
        if not is_company_email(email, self.domains):
            raise AppError("EMAIL_DOMAIN_NOT_ALLOWED",
                           f"Please use your official @{self.domains[0]} e-mail address.")
        return email

    @staticmethod
    def _limit(limiter: RateLimiter, key: str) -> None:
        if not limiter.allow(key):
            wait = max(1, limiter.retry_after(key) // 60)
            raise AppError("RATE_LIMITED", f"Too many attempts. Please try again in about {wait} minute(s).")

    @staticmethod
    def _check_password(password: str) -> None:
        problems = password_problems(password or "")
        if problems:
            raise AppError("WEAK_PASSWORD", "Password must contain " + ", ".join(problems) + ".")

    def _access(self) -> Access | None:
        from core.database import Repository

        data = Repository(self.client).get_my_access()
        return Access.from_dict(data) if data else None

    # ------------------------------------------------------------------ flows
    def sign_in(self, email: str, password: str) -> Access | None:
        email = self._require_company_email(email)
        self._limit(self.limiters.login, email)
        if not password:
            raise AppError("MISSING_FIELDS", "Please enter your password.")
        try:
            self.client.auth.sign_in_with_password({"email": email, "password": password})
        except Exception as exc:  # noqa: BLE001
            raise from_auth(exc) from None
        access = self._access()
        if access is None or normalize_email(access.email) != email:
            self.sign_out()
            raise AppError("EMPLOYEE_NOT_AUTHORIZED")
        self.limiters.login.reset(email)
        return access

    def sign_up(self, email: str, password: str, full_name: str) -> str:
        email = self._require_company_email(email)
        self._check_password(password)
        full_name = (full_name or "").strip()[:120]
        if len(full_name) < 2:
            raise AppError("MISSING_FIELDS", "Please enter your full name.")
        self._limit(self.limiters.signup, email)
        options: dict = {"data": {"full_name": full_name}}
        if self.redirect_url:
            options["email_redirect_to"] = self.redirect_url
        try:
            self.client.auth.sign_up({"email": email, "password": password, "options": options})
        except Exception as exc:  # noqa: BLE001
            err = from_auth(exc)
            if err.code in ("user_already_exists", "email_exists"):
                return GENERIC_SIGNUP  # do not reveal which addresses have accounts
            raise err from None
        return GENERIC_SIGNUP

    def verify_email(self, email: str, code: str) -> Access | None:
        email = self._require_company_email(email)
        code = "".join(ch for ch in (code or "") if ch.isdigit())
        if len(code) < 6:
            raise AppError("otp_expired", "Enter the 6-digit code from the e-mail.")
        self._limit(self.limiters.otp, email)
        last: Exception | None = None
        for otp_type in ("email", "signup"):
            try:
                self.client.auth.verify_otp({"email": email, "token": code, "type": otp_type})
                return self._access()
            except Exception as exc:  # noqa: BLE001
                last = exc
        raise from_auth(last) from None

    def resend_verification(self, email: str) -> str:
        email = self._require_company_email(email)
        self._limit(self.limiters.signup, email)
        try:
            self.client.auth.resend({"type": "signup", "email": email})
        except Exception as exc:  # noqa: BLE001
            log.info("resend verification failed: %s", type(exc).__name__)
        return GENERIC_SIGNUP

    def request_password_reset(self, email: str) -> str:
        email = normalize_email(email)
        if not is_company_email(email, self.domains):
            return GENERIC_RESET
        self._limit(self.limiters.reset, email)
        try:
            options = {"redirect_to": self.redirect_url} if self.redirect_url else None
            self.client.auth.reset_password_for_email(email, options)
        except Exception as exc:  # noqa: BLE001
            log.info("password reset request failed: %s", type(exc).__name__)
        return GENERIC_RESET

    def reset_password(self, email: str, code: str, new_password: str) -> None:
        email = self._require_company_email(email)
        self._check_password(new_password)
        self._limit(self.limiters.otp, email)
        try:
            self.client.auth.verify_otp({"email": email, "token": (code or "").strip(), "type": "recovery"})
            self.client.auth.update_user({"password": new_password})
        except Exception as exc:  # noqa: BLE001
            raise from_auth(exc) from None
        finally:
            self.sign_out()

    def change_password(self, new_password: str) -> None:
        self._check_password(new_password)
        try:
            self.client.auth.update_user({"password": new_password})
        except Exception as exc:  # noqa: BLE001
            raise from_auth(exc) from None

    def refresh_access(self) -> Access | None:
        return self._access()

    def sign_out(self) -> None:
        try:
            self.client.auth.sign_out()
        except Exception:  # noqa: BLE001 - signing out locally must always succeed
            log.debug("sign_out raised; ignoring")
