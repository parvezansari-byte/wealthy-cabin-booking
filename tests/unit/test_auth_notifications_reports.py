from datetime import date, time, timezone
from types import SimpleNamespace

import pytest

from core.auth import Access, AuthService, Limiters, evaluate_access
from core.booking_service import Rules
from core.errors import AppError
from core.notifications import NotificationWorker, render_email
from core.ratelimit import RateLimiter
from core.reports import bookings_csv, office_minutes, utilization

UTC = timezone.utc
DOMAINS = ("wealthy.in",)


# ------------------------------------------------------------------ auth
class FakeAuthApi:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args))
            if self.fail and name in self.fail:
                raise self.fail[name]
        return call


class FakeClient:
    def __init__(self, access=None, fail=None):
        self.auth = FakeAuthApi(fail)
        self._access = access

    def rpc(self, fn, params):
        assert fn == "get_my_access"
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=self._access))


ACTIVE = {"user_id": "u1", "email": "asha@wealthy.in", "full_name": "Asha", "role": "employee",
          "status": "active", "email_verified": True, "domain_allowed": True, "is_employee": True,
          "is_admin": False}


def svc(access=ACTIVE, fail=None, limiters=None):
    client = FakeClient(access, fail)
    return AuthService(client, DOMAINS, limiters or Limiters.default()), client


def test_sign_in_rejects_personal_email_before_calling_supabase():
    service, client = svc()
    with pytest.raises(AppError) as exc:
        service.sign_in("asha@gmail.com", "whatever-123")
    assert exc.value.code == "EMAIL_DOMAIN_NOT_ALLOWED"
    assert client.auth.calls == []


def test_sign_in_success_returns_access():
    service, client = svc()
    access = service.sign_in("Asha@Wealthy.in", "Str0ng-enough-pass")
    assert access.is_employee and client.auth.calls[0][0] == "sign_in_with_password"
    assert client.auth.calls[0][1][0]["email"] == "asha@wealthy.in"


class _AuthErr(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.message, self.code = message, code


def test_unverified_email_is_refused():
    service, _ = svc(fail={"sign_in_with_password": _AuthErr("Email not confirmed", "email_not_confirmed")})
    with pytest.raises(AppError) as exc:
        service.sign_in("asha@wealthy.in", "Str0ng-enough-pass")
    assert exc.value.code == "email_not_confirmed"


def test_login_rate_limited_per_email():
    limiters = Limiters(login=RateLimiter(2, 900), signup=RateLimiter(3, 3600),
                        otp=RateLimiter(3, 900), reset=RateLimiter(3, 3600))
    service, _ = svc(fail={"sign_in_with_password": _AuthErr("Invalid login credentials", "invalid_credentials")},
                     limiters=limiters)
    for _ in range(2):
        with pytest.raises(AppError) as exc:
            service.sign_in("asha@wealthy.in", "wrong-pass-1")
        assert exc.value.code == "invalid_credentials"
    with pytest.raises(AppError) as exc:
        service.sign_in("asha@wealthy.in", "wrong-pass-1")
    assert exc.value.code == "RATE_LIMITED"


def test_signup_requires_company_email_and_strong_password():
    service, client = svc()
    with pytest.raises(AppError):
        service.sign_up("x@yahoo.com", "Str0ng-enough-pass", "X Person")
    with pytest.raises(AppError) as exc:
        service.sign_up("x@wealthy.in", "weak", "X Person")
    assert exc.value.code == "WEAK_PASSWORD"
    message = service.sign_up("x@wealthy.in", "Str0ng-enough-pass", "X Person")
    assert "verification code" in message
    sent = client.auth.calls[-1][1][0]
    assert sent["options"]["data"] == {"full_name": "X Person"}


def test_signup_does_not_reveal_existing_accounts():
    service, _ = svc(fail={"sign_up": _AuthErr("User already registered", "user_already_exists")})
    assert "verification code" in service.sign_up("x@wealthy.in", "Str0ng-enough-pass", "X Person")


def test_password_reset_is_silent_for_unknown_or_foreign_addresses():
    service, client = svc()
    assert "If an account exists" in service.request_password_reset("x@gmail.com")
    assert client.auth.calls == []


def test_evaluate_access_states():
    base = Access.from_dict(ACTIVE)
    assert evaluate_access(base, DOMAINS) == (True, "")
    assert not evaluate_access(None, DOMAINS)[0]
    pending = Access.from_dict({**ACTIVE, "status": "pending", "is_employee": False})
    assert "approve" in evaluate_access(pending, DOMAINS)[1]
    disabled = Access.from_dict({**ACTIVE, "status": "disabled", "is_employee": False})
    assert "disabled" in evaluate_access(disabled, DOMAINS)[1]
    unverified = Access.from_dict({**ACTIVE, "email_verified": False, "is_employee": False})
    assert "verify" in evaluate_access(unverified, DOMAINS)[1]
    foreign = Access.from_dict({**ACTIVE, "email": "asha@gmail.com"})
    assert not evaluate_access(foreign, DOMAINS)[0]
    # server env domain must also match, even if the database says OK
    assert not evaluate_access(base, ("other.com",))[0]


# ------------------------------------------------------------------ notifications
ITEM = {"id": "n1", "kind": "booking_confirmed", "recipient_email": "asha@wealthy.in", "attempts": 1,
        "booking_id": "b1", "reference": "WCB-100001", "booking_status": "confirmed",
        "title": "<script>alert(1)</script> Sync", "start_at": "2026-10-14T04:30:00+00:00",
        "end_at": "2026-10-14T05:30:00+00:00", "cancel_reason": None, "cabin_name": "Cabin 03",
        "cabin_location": "Floor 1", "employee_name": "Asha", "timezone": "Asia/Kolkata",
        "app_name": "Wealthy Office Cabin Booking", "company_name": "Wealthy"}


def test_email_contains_required_fields_and_is_escaped():
    subject, text, html = render_email(ITEM)
    assert "WCB-100001" in subject and "Cabin 03" in subject
    for needle in ("Cabin 03", "Wed, 14 Oct 2026", "10:00–11:00", "WCB-100001", "Sync"):
        assert needle in text
    assert "<script>" not in html and "&lt;script&gt;" in html


class FakeServiceClient:
    def __init__(self, items):
        self.items, self.completed = items, []

    def rpc(self, fn, params):
        if fn == "claim_notifications":
            data = self.items
            self.items = []
        elif fn == "complete_notification":
            self.completed.append((params["p_id"], params["p_outcome"], params.get("p_error")))
            data = None
        else:
            data = 0
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))


class Mailer:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, to, subject, text, html):
        if self.fail:
            import smtplib
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials for secret-user")
        self.sent.append((to, subject))


def test_worker_sends_and_marks_sent():
    client, mailer = FakeServiceClient([dict(ITEM)]), Mailer()
    assert NotificationWorker(client, mailer).process() == {"sent": 1, "failed": 0, "skipped": 0}
    assert client.completed == [("n1", "sent", None)] and mailer.sent[0][0] == "asha@wealthy.in"


def test_worker_records_failure_without_leaking_server_text():
    client = FakeServiceClient([dict(ITEM)])
    assert NotificationWorker(client, Mailer(fail=True)).process()["failed"] == 1
    nid, outcome, error = client.completed[0]
    assert outcome == "failed" and "SMTP 535" in error and "secret-user" not in error


def test_worker_skips_reminders_for_cancelled_bookings_and_when_disabled():
    client = FakeServiceClient([{**ITEM, "kind": "reminder", "booking_status": "cancelled"}])
    assert NotificationWorker(client, Mailer()).process()["skipped"] == 1
    client = FakeServiceClient([dict(ITEM)])
    assert NotificationWorker(client, Mailer(), enabled=False).process()["skipped"] == 1


# ------------------------------------------------------------------ reports
RULES = Rules(timezone="Asia/Kolkata", open_time=time(9), close_time=time(19), working_days=(1, 2, 3, 4, 5))
CABINS = [{"id": "c1", "code": "C01", "name": "Cabin 01"}, {"id": "c2", "code": "C02", "name": "Cabin 02"}]


def test_office_minutes_counts_working_days_only():
    assert office_minutes(RULES, date(2026, 10, 12), date(2026, 10, 18)) == 5 * 600


def test_utilization_and_csv():
    rows = [
        {"cabin_id": "c1", "status": "confirmed", "start_at": "2026-10-14T04:30:00+00:00",
         "end_at": "2026-10-14T06:30:00+00:00", "reference": "WCB-1", "title": "=cmd|' /C calc'!A0",
         "attendees": 3, "cabins": {"name": "Cabin 01"}, "owner": {"full_name": "Asha", "email": "a@wealthy.in"},
         "created_at": "2026-10-13T04:30:00+00:00", "notes": "secret"},
        {"cabin_id": "c1", "status": "cancelled", "start_at": "2026-10-14T08:30:00+00:00",
         "end_at": "2026-10-14T09:30:00+00:00", "reference": "WCB-2", "title": "x", "attendees": 1,
         "cabins": {"name": "Cabin 01"}, "owner": {}, "cancelled_at": "2026-10-13T09:00:00+00:00",
         "created_at": "2026-10-13T04:30:00+00:00"},
    ]
    report = {r["code"]: r for r in utilization(RULES, CABINS, rows, date(2026, 10, 14), date(2026, 10, 14))}
    assert report["C01"]["booked_minutes"] == 120 and report["C01"]["utilization_pct"] == 20.0
    assert report["C01"]["cancelled"] == 1 and report["C02"]["bookings"] == 0
    csv_text = bookings_csv(rows, RULES)
    assert "'=cmd" in csv_text            # formula neutralised
    assert "secret" not in csv_text       # notes excluded by default
    assert "secret" in bookings_csv(rows, RULES, include_notes=True)
    assert "10:00" in csv_text            # converted to office time
