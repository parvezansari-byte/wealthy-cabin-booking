"""End-to-end UI tests: the real Streamlit app (via streamlit.testing.AppTest)
against the real migrated database, through a PostgREST-like fake client.

Requires TEST_DATABASE_URL (see tests/db/conftest.py).
"""
from datetime import timedelta
from pathlib import Path

import pytest

from tests.db.conftest import database, db  # noqa: F401  (fixtures)
from tests.e2e.fake_supabase import OTP, FakeSupabase

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "Str0ng-enough-pass"


@pytest.fixture()
def app_env(db, monkeypatch):  # noqa: F811
    import config
    import ui.context

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "test-anon-key")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAIN", "wealthy.in")
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    config.get_config.cache_clear()
    passwords: dict[str, str] = {}
    monkeypatch.setattr(ui.context, "create_user_client", lambda url, key: FakeSupabase(db.conninfo, passwords))
    yield db, passwords
    config.get_config.cache_clear()


def _labelled(widgets, label):
    for w in widgets:
        if w.label == label:
            return w
    raise AssertionError(f"no widget labelled {label!r}: {[w.label for w in widgets]}")


def _texts(at) -> str:
    parts = [m.value for m in at.markdown] + [e.value for e in at.error] + [s.value for s in at.success] \
        + [i.value for i in at.info] + [c.value for c in at.caption] + [w.value for w in at.warning] \
        + [h.value for h in at.subheader] + [h.value for h in at.header] + [t.label for t in at.tabs]
    return "\n".join(str(p) for p in parts)


def _app():
    return AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)


def _view(monkeypatch, view, email):
    monkeypatch.setenv("WCB_E2E_VIEW", view)
    monkeypatch.setenv("WCB_E2E_EMAIL", email)
    monkeypatch.setenv("WCB_E2E_PASSWORD", PASSWORD)
    return AppTest.from_file(str(ROOT / "tests" / "e2e" / "harness.py"), default_timeout=60)


def test_login_page_rejects_personal_email_and_signs_in_employee(app_env):
    db, passwords = app_env
    db.employee("asha.rao@wealthy.in", name="Asha Rao")
    passwords["asha.rao@wealthy.in"] = PASSWORD

    at = _app().run()
    assert not at.exception
    assert "Wealthy Office Cabin Booking" in _texts(at)

    _labelled(at.text_input, "Work e-mail").input("asha@gmail.com")
    _labelled(at.text_input, "Password").input(PASSWORD)
    _labelled(at.button, "Sign in").click()
    at.run()
    assert "official @wealthy.in" in _texts(at)

    _labelled(at.text_input, "Work e-mail").input("asha.rao@wealthy.in")
    _labelled(at.text_input, "Password").input("wrong-password-1")
    _labelled(at.button, "Sign in").click()
    at.run()
    assert "Incorrect e-mail or password" in _texts(at)

    _labelled(at.text_input, "Password").input(PASSWORD)
    _labelled(at.button, "Sign in").click()
    at.run()
    assert not at.exception, at.exception
    text = _texts(at)
    assert "Asha Rao" in text and "Total cabins" in text and "Cabin 10" in text


def test_signup_verify_then_pending_until_approved(app_env):
    db, passwords = app_env
    at = _app().run()
    _labelled(at.text_input, "Full name").input("New Joiner")
    signup_email = [w for w in at.text_input if w.key == "su_email"][0]
    signup_email.input("someone@yahoo.com")
    [w for w in at.text_input if w.key == "su_pw1"][0].input(PASSWORD)
    [w for w in at.text_input if w.key == "su_pw2"][0].input(PASSWORD)
    _labelled(at.button, "Create account").click()
    at.run()
    assert "official @wealthy.in" in _texts(at)
    assert db.owner("select count(*) from auth.users")[0][0] == 0

    [w for w in at.text_input if w.key == "su_email"][0].input("new.joiner@wealthy.in")
    _labelled(at.button, "Create account").click()
    at.run()
    assert "verification code" in _texts(at)

    [w for w in at.text_input if w.key == "vf_code"][0].input(OTP)
    _labelled(at.button, "Verify").click()
    at.run()
    assert not at.exception
    assert "needs to approve your access" in _texts(at)  # verified, but not yet authorised

    admin = db.admin()
    uid = db.owner("select id from cabins.profiles where email = 'new.joiner@wealthy.in'")[0][0]
    db.call(admin, "select cabins.admin_set_employee_status(%s, 'active')", (str(uid),))
    _labelled(at.button, "Check again").click()
    at.run()
    assert "Total cabins" in _texts(at)


def test_book_through_dialog_and_cancel(app_env, monkeypatch):
    db, passwords = app_env
    uid = db.employee("booker@wealthy.in", name="Booker")
    passwords["booker@wealthy.in"] = PASSWORD

    at = _view(monkeypatch, "cabins", "booker@wealthy.in").run()
    assert not at.exception, at.exception
    assert "Day timeline" in _texts(at)
    _labelled(at.button, "Book Cabin 03").click()
    at.run()
    assert not at.exception, at.exception
    _labelled(at.text_input, "Meeting title / purpose *").input("Design review")
    _labelled(at.button, "Confirm booking").click()
    at.run()
    assert not at.exception, at.exception
    assert "Reference **WCB-" in _texts(at) or "Reference WCB-" in _texts(at)
    rows = db.owner("select title, status from cabins.bookings where user_id = %s", (uid,))
    assert rows == [("Design review", "confirmed")]

    at = _view(monkeypatch, "my_bookings", "booker@wealthy.in").run()
    assert "Design review" in _texts(at)
    _labelled(at.button, "Cancel").click()
    at.run()
    _labelled(at.button, "Yes, cancel it").click()
    at.run()
    assert not at.exception, at.exception
    assert db.owner("select status from cabins.bookings where user_id = %s", (uid,))[0][0] == "cancelled"


def test_conflicting_booking_shows_clear_message(app_env, monkeypatch):
    db, passwords = app_env
    other = db.employee()
    db.employee("second@wealthy.in")
    passwords["second@wealthy.in"] = PASSWORD
    at = _view(monkeypatch, "cabins", "second@wealthy.in").run()
    # Someone else books Cabin 01 for the dialog's default slot while this user has the page open.
    _labelled(at.button, "Book Cabin 01").click()
    at.run()
    start_choice = _labelled(at.selectbox, "Start time").value
    day = _labelled(at.date_input, "Meeting date").value
    from datetime import datetime
    from zoneinfo import ZoneInfo
    start = datetime.combine(day, start_choice).replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    db.book(other, db.cabin("C01")["id"], start, start + timedelta(minutes=60))
    _labelled(at.text_input, "Meeting title / purpose *").input("Clashing meeting")
    _labelled(at.button, "Confirm booking").click()
    at.run()
    assert "already booked" in _texts(at)
    assert db.owner("select count(*) from cabins.bookings")[0][0] == 1


def test_every_page_renders_for_employee_and_admin(app_env, monkeypatch):
    db, passwords = app_env
    db.employee("emp@wealthy.in")
    db.admin("boss@wealthy.in")
    passwords.update({"emp@wealthy.in": PASSWORD, "boss@wealthy.in": PASSWORD})
    db.create_user("waiting@wealthy.in")  # a pending account for the admin view
    for view in ("employee_dashboard", "cabins", "my_bookings", "account"):
        at = _view(monkeypatch, view, "emp@wealthy.in").run()
        assert not at.exception, (view, at.exception)
    at = _view(monkeypatch, "admin_dashboard", "emp@wealthy.in").run()
    assert "Administrator access is required" in _texts(at)
    at = _view(monkeypatch, "admin_dashboard", "boss@wealthy.in").run()
    assert not at.exception, at.exception
    text = _texts(at)
    assert "Utilisation report" in text and "waiting for approval" in text


def test_admin_approves_and_edits_cabin_through_ui(app_env, monkeypatch):
    db, passwords = app_env
    db.admin("boss@wealthy.in")
    passwords["boss@wealthy.in"] = PASSWORD
    pending = db.create_user("waiting@wealthy.in")
    at = _view(monkeypatch, "admin_dashboard", "boss@wealthy.in").run()
    _labelled(at.selectbox, "Show").select("pending")
    at.run()
    _labelled(at.button, "✅ Approve / enable access").click()
    at.run()
    assert not at.exception, at.exception
    assert db.status(pending) == "active"
    assert "can now book cabins" in _texts(at)
