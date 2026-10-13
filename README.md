# Wealthy Office Cabin Booking

Book one of Wealthy's ten meeting cabins for up to two hours. Only verified, approved `@wealthy.in` employees can sign in; double bookings are impossible because PostgreSQL itself refuses overlapping reservations.

**Stack**: Streamlit (Python 3.12) · Supabase PostgreSQL, Auth and PostgREST · Render · any SMTP provider.

| Document | What's in it |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Stage 1: architecture, security model, RLS ↔ Auth, schema, booking rules, SSO options |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Supabase + Render step-by-step, first admin, acceptance tests, custom domain |
| [docs/SECURITY.md](docs/SECURITY.md) | Go-live checklist, controls, backup/restore, incident playbooks |

## Features

* **Employees**: dashboard (who's signed in, today, office time zone, available/occupied now, upcoming and history, Quick Book, search and filters); availability page with 10 cabin cards, date/start/duration/seat/equipment filters, auto-refresh and a day timeline of reserved and free slots; booking dialog with calculated end time and live availability; My bookings with Upcoming / Completed / Cancelled, search, references and cancellation with confirmation.
* **Admins**: occupancy and utilisation report (chart + CSV), all bookings with filters, CSV export and cancellation, employee approval / disable / enable / role changes, allowlist, cabin editor, maintenance blocks (optionally cancelling and notifying clashing bookings), office hours and booking rules, branding, audit log (CSV), e-mail delivery status with retry.
* **Statuses** use colour **and** text and a symbol: ● Available (green), ■ Booked (red), ★ Your booking (blue), ▲ Maintenance / ○ Inactive (grey).
* **E-mails**: confirmation, cancellation, admin cancellation, maintenance cancellation, reminders — each with cabin, date, start, end, title and reference.

## Project layout

```
app.py                     Streamlit entry point: auth gate, navigation, sidebar
config.py                  environment-variable configuration (no secrets in code)
core/
  auth.py                  sign-in / sign-up / verify / reset on Supabase Auth, access evaluation
  booking_service.py       booking rules, availability maths, booking & cancellation use cases
  database.py              Supabase clients and the RLS-bound repository
  notifications.py         e-mail rendering, SMTP sender, outbox worker
  reports.py               utilisation and CSV export
  errors.py · security.py · ratelimit.py · timeutil.py
ui/                        theme (CSS), components (cards, badges, timeline), dialogs, session context
views/                     login, employee_dashboard, cabins, my_bookings, account, admin_dashboard
database/
  migrations/0001…0006     versioned schema, functions, RLS, seed data (10 cabins)
  scripts/bootstrap_first_admin.sql
  local/supabase_stub.sql  LOCAL TESTS ONLY (minimal auth schema + roles)
scripts/
  apply_migrations.py      applies migrations once each, with checksums
  process_notifications.py e-mail worker (Render cron job)
tests/
  unit/                    pure Python: rules, time zones/DST, auth flows, e-mail, CSV, config
  db/                      real PostgreSQL: RLS, constraints, functions, concurrency
  e2e/                     the Streamlit app driven by AppTest against the real database
render.yaml · requirements.txt · .env.example · .streamlit/config.toml · .github/workflows/ci.yml
```

> `views/` is used instead of Streamlit's magic `pages/` folder so that page visibility (e.g. Administration) is decided per user in `app.py`.

## Local development

You need Python 3.12 and a Supabase project. The easiest local backend is the Supabase CLI (Docker), which gives you real Auth, PostgREST and a mail catcher:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env                       # fill in values

# Option A: local Supabase (Docker)
supabase init && supabase start            # prints API URL, anon key, service_role key
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54322/postgres
python scripts/apply_migrations.py
# put the printed URL/keys in .env (SUPABASE_URL=http://127.0.0.1:54321)
# e-mails (verification codes) appear in the local mail UI the CLI prints (Inbucket/Mailpit)

# Option B: a separate cloud "dev" Supabase project — see docs/DEPLOYMENT.md steps 2–4

streamlit run app.py                       # http://localhost:8501
```

Create the first admin: sign up in the app, enter the code, then run `select cabins_private.bootstrap_first_admin('you@wealthy.in');` against the database.

To send e-mails locally, set the `SMTP_*` variables and `SUPABASE_SERVICE_ROLE_KEY`, or run the worker by hand: `python -m scripts.process_notifications`.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/unit                      # no database needed

# Database + end-to-end tests need a disposable PostgreSQL 15+ superuser connection.
# The suite creates and drops its own temporary database.
docker run -d --name wcb-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16
export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres
python -m pytest                                  # everything
```

Without `TEST_DATABASE_URL`, the `db` and `e2e` tests are skipped (reported as skipped, not passed). CI (`.github/workflows/ci.yml`) runs everything against a PostgreSQL 16 service container on every push and pull request.

| Requirement | Tests |
|---|---|
| Authorised login; unauthorised and unverified e-mails rejected | `unit/test_auth_notifications_reports.py`, `db/test_authentication_db.py`, `e2e/test_app_e2e.py` |
| Employee vs admin separation; self-promotion blocked; first-admin bootstrap | `db/test_authentication_db.py`, `db/test_admin_and_notifications_db.py` |
| Successful booking; > 120 min; overlap; adjacent; outside hours; past; maintenance; capacity; inactive | `db/test_booking_rules_db.py`, `unit/test_booking_logic.py` |
| Two (and twelve) simultaneous attempts on the same cabin | `db/test_concurrency_db.py` |
| Employee / unauthorised / admin cancellation, cut-off | `db/test_cancellation_and_rls_db.py` |
| RLS enforcement (own rows, hidden details, no direct writes, anon, logs) | `db/test_cancellation_and_rls_db.py` |
| Database and notification failures | `unit/test_booking_logic.py`, `unit/test_auth_notifications_reports.py`, `db/test_admin_and_notifications_db.py` |
| Time zones and daylight saving | `unit/test_booking_logic.py`, `db/test_booking_rules_db.py` |
| Full UI flows (sign-in, sign-up → verify → approval, book, conflict, cancel, admin approve) | `e2e/test_app_e2e.py` |

**What the automated tests do not cover**: the e2e tests replace Supabase Auth and PostgREST with a thin in-process stand-in (`tests/e2e/fake_supabase.py`) that runs every query against the real migrated database as the `authenticated` role, so RLS and functions are real but Supabase's own services, real e-mail delivery and Render are not exercised. Use the acceptance checklist in `docs/DEPLOYMENT.md` §12 after deploying.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| "The application is not configured correctly" | Missing `SUPABASE_URL`, `SUPABASE_ANON_KEY` or `ALLOWED_EMAIL_DOMAIN`, or an invalid `OFFICE_TIMEZONE`. Check Render → Environment and the logs. |
| Sign-up says only company addresses are allowed for a `@wealthy.in` address | `office_settings.allowed_email_domains` doesn't contain `wealthy.in`, or the env var has a typo. |
| Verification code never arrives | Supabase Auth is using the built-in mailer (rate-limited) — configure custom SMTP (Deployment §4.4); check spam; check Supabase → Logs → Auth. |
| Code rejected as invalid | The template lacks `{{ .Token }}` or the code expired; use *Resend code*. Clicking the e-mail link also verifies — then just sign in. |
| "Awaiting approval" after verifying | Expected under the `approval` policy. An admin approves under Administration → Employees, or adds the address to the allowlist. |
| Everyone is signed out after a deploy or refresh | Expected: Streamlit sessions are in memory. |
| Random sign-outs with more than one instance | Scale the Render service back to one instance. |
| "Something went wrong while talking to the server" | Supabase unreachable or an unexpected DB error; details are in the Render logs. Nothing was changed. |
| Bookings rejected as outside office hours unexpectedly | Check Admin › Settings time zone and working days; the database uses those, not the browser's zone. |
| E-mails stuck in *pending* | The cron job isn't running or `SMTP_*`/`SUPABASE_SERVICE_ROLE_KEY` are missing; see Admin › Notifications and the cron job logs. *Retry failed* re-queues failures. |
| Deleting a user in Supabase fails with a database error | By design (history is kept). Disable the employee in the app instead. |
| `apply_migrations.py`: "was modified after it was applied" | Don't edit applied migrations — add `0007_….sql`. |
| Migration connection times out | Use the Session pooler connection string (IPv4), not the direct one. |
