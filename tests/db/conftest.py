"""Fixtures for database tests.

These run the real migrations against a throw-away PostgreSQL database (with a
small Supabase stub for auth.users / auth.uid() / roles) and exercise the RLS
policies and booking functions exactly as PostgREST would: by switching to the
'authenticated' role with JWT claims set for the request.

Set TEST_DATABASE_URL to a PostgreSQL 15+ superuser connection, e.g.
    TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres
Tests are skipped when it is not set.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql  # noqa: E402
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
STUB = ROOT / "database" / "local" / "supabase_stub.sql"
SEED = ROOT / "database" / "migrations" / "0006_seed_cabins.sql"
IST = ZoneInfo("Asia/Kolkata")

BASE_URL = os.environ.get("TEST_DATABASE_URL")


class DbError(Exception):
    def __init__(self, code: str, sqlstate: str | None, detail: str | None):
        super().__init__(f"{code} ({sqlstate}): {detail}")
        self.code, self.sqlstate, self.detail = code, sqlstate, detail


class DB:
    def __init__(self, conninfo: str):
        self.conninfo = conninfo

    # -------------------------------------------------------------- owner (superuser)
    def owner(self, query: str, params=None):
        with psycopg.connect(self.conninfo, autocommit=True) as conn:
            cur = conn.execute(query, params)
            return cur.fetchall() if cur.description else None

    def create_user(self, email: str, confirmed: bool = True, full_name: str = "Test Person") -> str:
        rows = self.owner(
            "insert into auth.users (email, email_confirmed_at, raw_user_meta_data) "
            "values (%s, case when %s then now() end, %s) returning id",
            (email, confirmed, json.dumps({"full_name": full_name})))
        return str(rows[0][0])

    def confirm(self, uid: str) -> None:
        self.owner("update auth.users set email_confirmed_at = now() where id = %s", (uid,))

    def employee(self, email: str | None = None, name: str = "Test Employee") -> str:
        email = email or f"emp-{uuid.uuid4().hex[:8]}@wealthy.in"
        self.owner("insert into cabins.employee_allowlist (email) values (%s)", (email,))
        return self.create_user(email, True, name)

    def admin(self, email: str | None = None) -> str:
        uid = self.employee(email or f"admin-{uuid.uuid4().hex[:8]}@wealthy.in", "Test Admin")
        self.owner("update cabins.profiles set role = 'admin' where id = %s", (uid,))
        return uid

    def status(self, uid: str) -> str:
        return self.owner("select status from cabins.profiles where id = %s", (uid,))[0][0]

    def cabin(self, code: str = "C01") -> dict:
        row = self.owner("select id, name, capacity from cabins.cabins where code = %s", (code,))[0]
        return {"id": str(row[0]), "name": row[1], "capacity": row[2]}

    def settings(self, **changes) -> None:
        assignments = sql.SQL(", ").join(
            sql.SQL("{} = {}").format(sql.Identifier(k), sql.Literal(v)) for k, v in changes.items())
        self.owner(sql.SQL("update cabins.office_settings set {} where id").format(assignments).as_string())

    # -------------------------------------------------------------- as an API caller
    def call(self, uid: str | None, query: str, params=None, role: str = "authenticated"):
        """Run one statement the way PostgREST would for this user's JWT."""
        try:
            with psycopg.connect(self.conninfo) as conn:
                with conn.transaction():
                    claims = json.dumps({"sub": uid, "role": role}) if uid else ""
                    conn.execute("select set_config('request.jwt.claims', %s, true)", (claims,))
                    conn.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))
                    cur = conn.execute(query, params)
                    return cur.fetchall() if cur.description else cur.rowcount
        except psycopg.Error as exc:
            diag = exc.diag
            raise DbError(diag.message_primary or str(exc), diag.sqlstate, diag.message_detail) from None

    def book(self, uid: str, cabin_id: str, start: datetime, end: datetime, title: str = "Team sync",
             attendees: int = 2, notes: str | None = None) -> dict:
        rows = self.call(uid, "select cabins.book_cabin(%s, %s, %s, %s, %s, %s)",
                         (cabin_id, start, end, title, attendees, notes))
        return rows[0][0]


def slot(days: int = 1, hh: int = 10, mm: int = 0, minutes: int = 60, tz: ZoneInfo = IST):
    day = (datetime.now(tz) + timedelta(days=days)).date()
    start = datetime.combine(day, time(hh, mm)).replace(tzinfo=tz)
    return start, start + timedelta(minutes=minutes)


@pytest.fixture(scope="session")
def database():
    if not BASE_URL:
        pytest.skip("TEST_DATABASE_URL not set — database tests skipped")
    from scripts.apply_migrations import apply_migrations

    params = conninfo_to_dict(BASE_URL)
    name = f"wealthy_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(BASE_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    params["dbname"] = name
    conninfo = make_conninfo(**params)
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute(STUB.read_text())
    apply_migrations(conninfo, out=lambda *_: None)
    yield DB(conninfo)
    with psycopg.connect(BASE_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))


@pytest.fixture()
def db(database: DB) -> DB:
    database.owner("""
        truncate cabins.notification_logs, cabins.bookings, cabins.maintenance_blocks,
                 cabins.employee_allowlist, cabins.audit_logs, cabins.profiles, cabins.cabins,
                 auth.users cascade
    """)
    database.owner(SEED.read_text())
    # Deterministic, every-day office hours so tests do not depend on today's weekday.
    database.owner("""
        update cabins.office_settings set
          timezone = 'Asia/Kolkata', open_time = '08:00', close_time = '20:00',
          working_days = '{1,2,3,4,5,6,7}', min_booking_minutes = 15, max_booking_minutes = 120,
          slot_minutes = 15, booking_horizon_days = 60, cancel_cutoff_minutes = 0,
          access_policy = 'approval', show_booker_names = false, reminder_minutes_before = 15,
          allowed_email_domains = '{wealthy.in}'
        where id
    """)
    return database
