"""A small stand-in for the supabase-py client that talks to the real test
PostgreSQL database the way PostgREST does: every request runs as the
'authenticated' role with the user's JWT claims, so all RLS policies and
SECURITY DEFINER functions are exercised for real.

Only the query-builder features this app uses are implemented. Auth is
simulated (passwords held in memory, OTP code 123456); the Supabase Auth
service itself is not under test here.
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import psycopg
from psycopg import sql
from postgrest.exceptions import APIError

OTP = "123456"

RELATIONS = {  # (base table, embed target, fk hint) -> (fk column on base, target table)
    ("bookings", "cabins", None): ("cabin_id", "cabins"),
    ("bookings", "profiles", "bookings_user_id_fkey"): ("user_id", "profiles"),
    ("maintenance_blocks", "cabins", None): ("cabin_id", "cabins"),
    ("notification_logs", "bookings", None): ("booking_id", "bookings"),
}


class AuthApiError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.message, self.code = message, code


def _split_top(text: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


class FakeAuth:
    def __init__(self, backend: "FakeSupabase"):
        self.b = backend

    def _user(self, email):
        rows = self.b.owner("select id, email_confirmed_at from auth.users where lower(email) = lower(%s)", (email,))
        return rows[0] if rows else None

    def sign_in_with_password(self, creds):
        user = self._user(creds["email"])
        if not user or self.b.passwords.get(creds["email"].lower()) != creds["password"]:
            raise AuthApiError("Invalid login credentials", "invalid_credentials")
        if user[1] is None:
            raise AuthApiError("Email not confirmed", "email_not_confirmed")
        self.b.uid = str(user[0])

    def sign_up(self, creds):
        if self._user(creds["email"]):
            raise AuthApiError("User already registered", "user_already_exists")
        try:
            self.b.owner("insert into auth.users (email, raw_user_meta_data) values (%s, %s)",
                         (creds["email"], json.dumps(creds.get("options", {}).get("data", {}))))
        except psycopg.Error:
            raise AuthApiError("Database error saving new user", "unexpected_failure") from None
        self.b.passwords[creds["email"].lower()] = creds["password"]

    def verify_otp(self, params):
        user = self._user(params["email"])
        if not user or params["token"] != OTP:
            raise AuthApiError("Token has expired or is invalid", "otp_expired")
        self.b.owner("update auth.users set email_confirmed_at = coalesce(email_confirmed_at, now()) where id = %s",
                     (user[0],))
        self.b.uid = str(user[0])

    def resend(self, creds):
        return None

    def reset_password_for_email(self, email, options=None):
        return None

    def update_user(self, attrs):
        if self.b.uid is None:
            raise AuthApiError("Auth session missing!", "session_not_found")
        email = self.b.owner("select email from auth.users where id = %s", (self.b.uid,))[0][0]
        self.b.passwords[email.lower()] = attrs["password"]

    def sign_out(self, options=None):
        self.b.uid = None


class Query:
    def __init__(self, backend: "FakeSupabase", table: str):
        self.b, self.table = backend, table
        self.mode, self.columns, self.payload = "select", "*", None
        self.filters, self.orders, self.lim, self.off = [], [], None, None

    # builder API -------------------------------------------------------
    def select(self, columns="*"):
        self.mode, self.columns = "select", columns
        return self

    def update(self, payload):
        self.mode, self.payload = "update", payload
        return self

    def insert(self, payload):
        self.mode, self.payload = "insert", payload
        return self

    def delete(self):
        self.mode = "delete"
        return self

    def _f(self, col, op, val):
        self.filters.append((col, op, val))
        return self

    def eq(self, c, v): return self._f(c, "=", v)
    def gte(self, c, v): return self._f(c, ">=", v)
    def lt(self, c, v): return self._f(c, "<", v)
    def like(self, c, v): return self._f(c, "like", v)

    def order(self, col, desc=False):
        self.orders.append((col, desc))
        return self

    def limit(self, n):
        self.lim = n
        return self

    def range(self, a, b):
        self.off, self.lim = a, b - a + 1
        return self

    # SQL --------------------------------------------------------------
    def _where(self):
        if not self.filters:
            return sql.SQL(""), []
        parts, params = [], []
        for col, op, val in self.filters:
            parts.append(sql.SQL("t.{} {} %s").format(sql.Identifier(col), sql.SQL(op)))
            params.append(val)
        return sql.SQL(" where ") + sql.SQL(" and ").join(parts), params

    def _select_list(self):
        items = []
        for item in _split_top(self.columns):
            if item == "*":
                items.append(sql.SQL("t.*"))
                continue
            m = re.fullmatch(r"(?:(\w+):)?(\w+)(?:!(\w+))?\((.*)\)", item)
            if not m:
                items.append(sql.SQL("t.{}").format(sql.Identifier(item)))
                continue
            alias, target, hint, cols = m.groups()
            fk, target_table = RELATIONS[(self.table, target, hint)]
            col_sql = sql.SQL(", ").join(sql.SQL("e0.{}").format(sql.Identifier(c.strip()))
                                         for c in cols.split(","))
            items.append(sql.SQL("(select to_jsonb(e) from (select {} from public.{} e0 where e0.id = t.{}) e) as {}")
                         .format(col_sql, sql.Identifier(target_table), sql.Identifier(fk),
                                 sql.Identifier(alias or target)))
        return sql.SQL(", ").join(items)

    def execute(self):
        where, params = self._where()
        tbl = sql.Identifier(self.table)
        if self.mode == "select":
            order = sql.SQL("")
            if self.orders:
                order = sql.SQL(" order by ") + sql.SQL(", ").join(
                    sql.SQL("t.{} {}").format(sql.Identifier(c), sql.SQL("desc" if d else "asc"))
                    for c, d in self.orders)
            page = sql.SQL("")
            if self.lim is not None:
                page += sql.SQL(" limit {}").format(sql.Literal(self.lim))
            if self.off:
                page += sql.SQL(" offset {}").format(sql.Literal(self.off))
            query = sql.SQL("select coalesce(jsonb_agg(to_jsonb(q)), '[]') from "
                            "(select {} from public.{} t{}{}{}) q").format(
                self._select_list(), tbl, where, order, page)
        elif self.mode == "update":
            sets = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in self.payload)
            params = list(self.payload.values()) + params
            query = sql.SQL("with t as (update public.{} t set {}{} returning t.*) "
                            "select coalesce(jsonb_agg(to_jsonb(t)), '[]') from t").format(tbl, sets, where)
        elif self.mode == "insert":
            cols = sql.SQL(", ").join(map(sql.Identifier, self.payload))
            vals = sql.SQL(", ").join(sql.Placeholder() for _ in self.payload)
            params = list(self.payload.values())
            query = sql.SQL("with t as (insert into public.{} ({}) values ({}) returning *) "
                            "select coalesce(jsonb_agg(to_jsonb(t)), '[]') from t").format(tbl, cols, vals)
        else:
            query = sql.SQL("with t as (delete from public.{} t{} returning t.*) "
                            "select coalesce(jsonb_agg(to_jsonb(t)), '[]') from t").format(tbl, where)
        return SimpleNamespace(data=self.b.as_user(query, params))


class Rpc:
    def __init__(self, backend, fn, params):
        self.b, self.fn, self.params = backend, fn, params or {}

    def execute(self):
        retset = self.b.owner("select proretset from pg_proc where proname = %s and pronamespace = 'public'::regnamespace",
                              (self.fn,))
        args = sql.SQL(", ").join(sql.SQL("{} => %s").format(sql.Identifier(k)) for k in self.params)
        call = sql.SQL("public.{}({})").format(sql.Identifier(self.fn), args)
        if retset and retset[0][0]:
            query = sql.SQL("select coalesce(jsonb_agg(to_jsonb(f)), '[]') from {} f").format(call)
        else:
            query = sql.SQL("select to_jsonb({})").format(call)
        return SimpleNamespace(data=self.b.as_user(query, list(self.params.values())))


class FakeSupabase:
    def __init__(self, conninfo: str, passwords: dict[str, str] | None = None, service_role: bool = False):
        self.conninfo = conninfo
        self.service_role = service_role
        self.passwords = passwords if passwords is not None else {}
        self.uid: str | None = None
        self.auth = FakeAuth(self)

    def owner(self, query, params=None):
        with psycopg.connect(self.conninfo, autocommit=True) as conn:
            cur = conn.execute(query, params)
            return cur.fetchall() if cur.description else None

    def as_user(self, query, params):
        role = "service_role" if self.service_role else ("authenticated" if self.uid else "anon")
        claims = json.dumps({"sub": self.uid, "role": role}) if self.uid else ""
        try:
            with psycopg.connect(self.conninfo) as conn:
                with conn.transaction():
                    conn.execute("select set_config('request.jwt.claims', %s, true)", (claims,))
                    conn.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))
                    return conn.execute(query, params).fetchone()[0]
        except psycopg.Error as exc:
            d = exc.diag
            raise APIError({"code": d.sqlstate, "message": d.message_primary, "details": d.message_detail,
                            "hint": d.message_hint}) from None

    def table(self, name):
        return Query(self, name)

    def rpc(self, fn, params=None):
        return Rpc(self, fn, params)
