"""Who may become an employee: domain, verification, approval, roles."""
import pytest

from tests.db.conftest import DbError, slot


@pytest.mark.parametrize("email", [
    "someone@gmail.com", "someone@yahoo.com", "someone@outlook.com",
    "someone@evilwealthy.in", "someone@wealthy.in.attacker.com", "someone@sub.wealthy.in",
    "not-an-email",
])
def test_non_company_addresses_cannot_register(db, email):
    with pytest.raises(Exception) as exc:
        db.create_user(email)
    assert "EMAIL_DOMAIN_NOT_ALLOWED" in str(exc.value)


def test_company_address_is_case_insensitive(db):
    uid = db.create_user("Mixed.Case@Wealthy.IN", confirmed=False)
    assert db.owner("select email from public.profiles where id = %s", (uid,))[0][0] == "mixed.case@wealthy.in"


def test_unverified_company_address_has_no_access(db):
    uid = db.create_user("unverified@wealthy.in", confirmed=False)
    db.owner("insert into public.employee_allowlist (email) values ('unverified@wealthy.in')")
    assert db.status(uid) == "pending"
    access = db.call(uid, "select public.get_my_access()")[0][0]
    assert access["email_verified"] is False and access["is_employee"] is False
    start, end = slot()
    with pytest.raises(DbError) as exc:
        db.book(uid, db.cabin()["id"], start, end)
    assert exc.value.code == "EMPLOYEE_NOT_AUTHORIZED"


def test_verified_but_unapproved_address_stays_pending(db):
    """A verified @wealthy.in address alone does not grant access under 'approval'."""
    uid = db.create_user("newjoiner@wealthy.in", confirmed=True)
    assert db.status(uid) == "pending"
    assert db.call(uid, "select public.get_my_access()")[0][0]["is_employee"] is False
    assert db.call(uid, "select count(*) from public.cabins")[0][0] == 0  # RLS hides everything


def test_allowlisted_address_activates_only_after_verification(db):
    db.owner("insert into public.employee_allowlist (email) values ('listed@wealthy.in')")
    uid = db.create_user("listed@wealthy.in", confirmed=False)
    assert db.status(uid) == "pending"
    db.confirm(uid)
    assert db.status(uid) == "active"
    assert db.call(uid, "select public.get_my_access()")[0][0]["is_employee"] is True


def test_allowlisting_after_verification_activates(db):
    uid = db.create_user("later@wealthy.in", confirmed=True)
    assert db.status(uid) == "pending"
    db.owner("insert into public.employee_allowlist (email) values ('later@wealthy.in')")
    assert db.status(uid) == "active"


def test_domain_policy_activates_verified_addresses(db):
    db.settings(access_policy="domain")
    uid = db.create_user("auto@wealthy.in", confirmed=False)
    assert db.status(uid) == "pending"
    db.confirm(uid)
    assert db.status(uid) == "active"


def test_allowlist_rejects_other_domains(db):
    with pytest.raises(Exception) as exc:
        db.owner("insert into public.employee_allowlist (email) values ('x@gmail.com')")
    assert "EMAIL_DOMAIN_NOT_ALLOWED" in str(exc.value)


def test_cannot_change_email_to_personal_address(db):
    uid = db.employee("mover@wealthy.in")
    with pytest.raises(Exception) as exc:
        db.owner("update auth.users set email = 'mover@gmail.com' where id = %s", (uid,))
    assert "EMAIL_DOMAIN_NOT_ALLOWED" in str(exc.value)


def test_banned_or_deleted_auth_user_loses_access(db):
    uid = db.employee()
    db.owner("update auth.users set banned_until = now() + interval '1 day' where id = %s", (uid,))
    assert db.call(uid, "select public.get_my_access()")[0][0]["is_employee"] is False


def test_disabled_employee_is_never_reactivated_automatically(db):
    admin = db.admin()
    uid = db.employee("leaver@wealthy.in")
    db.call(admin, "select public.admin_set_employee_status(%s, 'disabled')", (uid,))
    db.owner("update auth.users set email_confirmed_at = now() where id = %s", (uid,))
    assert db.status(uid) == "disabled"


def test_employee_cannot_promote_themselves(db):
    uid = db.employee()
    with pytest.raises(DbError) as exc:
        db.call(uid, "update public.profiles set role = 'admin' where id = %s", (uid,))
    assert exc.value.sqlstate == "42501"  # no column privilege on role
    with pytest.raises(DbError):
        db.call(uid, "update public.profiles set status = 'active' where id = %s", (uid,))
    assert db.owner("select role from public.profiles where id = %s", (uid,))[0][0] == "employee"


def test_employee_can_edit_own_name_only(db):
    me, other = db.employee(), db.employee()
    db.call(me, "update public.profiles set full_name = 'New Name' where id = %s", (me,))
    assert db.owner("select full_name from public.profiles where id = %s", (me,))[0][0] == "New Name"
    changed = db.call(me, "update public.profiles set full_name = 'Hacked' where id = %s", (other,))
    assert changed == 0


def test_bootstrap_admin_is_not_callable_through_the_api(db):
    uid = db.employee("boss@wealthy.in")
    with pytest.raises(DbError) as exc:
        db.call(uid, "select private.bootstrap_first_admin('boss@wealthy.in')")
    assert exc.value.sqlstate == "42501"


def test_bootstrap_first_admin_flow(db):
    uid = db.create_user("first.admin@wealthy.in", confirmed=False)
    with pytest.raises(Exception) as exc:
        db.owner("select private.bootstrap_first_admin('first.admin@wealthy.in')")
    assert "EMAIL_NOT_VERIFIED" in str(exc.value)
    db.confirm(uid)
    db.owner("select private.bootstrap_first_admin('first.admin@wealthy.in')")
    access = db.call(uid, "select public.get_my_access()")[0][0]
    assert access["is_admin"] is True and access["status"] == "active"
    db.create_user("second@wealthy.in")
    with pytest.raises(Exception) as exc:
        db.owner("select private.bootstrap_first_admin('second@wealthy.in')")
    assert "ADMIN_ALREADY_EXISTS" in str(exc.value)


def test_anonymous_callers_have_no_data_access(db):
    db.employee()
    for table in ("profiles", "cabins", "bookings", "audit_logs", "office_settings"):
        with pytest.raises(DbError) as exc:
            db.call(None, f"select * from public.{table}", role="anon")
        assert exc.value.sqlstate == "42501"
    with pytest.raises(DbError):
        db.call(None, "select public.get_my_access()", role="anon")
    branding = db.call(None, "select public.get_public_branding()", role="anon")[0][0]
    assert branding["app_name"] == "Wealthy Office Cabin Booking"
