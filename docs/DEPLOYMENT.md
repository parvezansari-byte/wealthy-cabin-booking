# Deployment guide — Supabase + Render

Time needed: about an hour. Dashboard labels below match Supabase and Render at the time of writing; if a label has moved, search the dashboard for the setting name.

## 1. Create the GitHub repository

```bash
cd wealthy-cabin-booking
git init && git add . && git commit -m "Wealthy Office Cabin Booking"
git branch -M main
git remote add origin git@github.com:<your-org>/wealthy-cabin-booking.git
git push -u origin main
```

Check that `.env` is **not** tracked (`git ls-files | grep .env` should print only `.env.example`). Make the repository private.

## 2. Set up Supabase

1. Create a project at supabase.com. Pick the **Mumbai (ap-south-1)** region for users in India. Save the database password in your password manager.
2. **Project Settings → API**: copy the *Project URL* (`SUPABASE_URL`), the *anon / publishable* key (`SUPABASE_ANON_KEY`) and the *service_role / secret* key (`SUPABASE_SERVICE_ROLE_KEY`). The service-role key bypasses all security — store it only in Render's environment, never in code, chat or tickets.
3. **Project Settings → Database → Connection string**: copy the **Session pooler** URI as `DATABASE_URL` (it works from IPv4 networks; the direct connection is IPv6 only on most plans).

## 3. Apply database migrations

From your laptop (Python 3.12):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL='postgresql://postgres.<ref>:<password>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres'
python scripts/apply_migrations.py --dry-run   # lists 0001…0006
python scripts/apply_migrations.py
```

Alternative: paste each file in `database/migrations/` **in order** into **SQL Editor** and run it (then the runner's bookkeeping table will not know about them — pick one method and stick to it).

Verify in SQL Editor:

```sql
select code, name, capacity from public.cabins order by code;          -- 10 rows
select allowed_email_domains, timezone, access_policy from public.office_settings;
select tablename, rowsecurity from pg_tables where schemaname = 'public'; -- all true
```

**Never run `database/local/supabase_stub.sql` on Supabase** — it is only for local tests.

## 4. Configure authentication and the permitted domain

In **Authentication**:

1. **Sign In / Providers → Email**: enable the Email provider, turn **Confirm email ON**, keep "Allow new users to sign up" ON (the database trigger refuses non-`@wealthy.in` addresses, and new accounts still need approval). Set minimum password length to **10** or more.
2. **URL Configuration**: *Site URL* = your app URL (e.g. `https://cabins.wealthy.in` or the `onrender.com` URL). Add the same URL under *Redirect URLs*.
3. **Emails → Templates**: the app verifies with a **6-digit code**, so add the code to two templates (keep the link too — clicking it also verifies):
   * *Confirm signup*: `<p>Your verification code is <strong>{{ .Token }}</strong></p>`
   * *Reset password*: `<p>Your password reset code is <strong>{{ .Token }}</strong></p>`
4. **Emails → SMTP settings**: configure **custom SMTP** (same provider as step 5). Supabase's built-in mailer is heavily rate limited and is not meant for production.
5. **Rate limits / Attack protection**: keep defaults or tighten; enable CAPTCHA if the app is ever exposed outside the company network.
6. The allowed domain lives in two places, and **both** must allow an address:
   * Render env var `ALLOWED_EMAIL_DOMAIN=wealthy.in`
   * Database: `office_settings.allowed_email_domains` (seeded as `{wealthy.in}`). To change it, run in SQL Editor (it cannot be changed through the app, by design):
     ```sql
     update public.office_settings set allowed_email_domains = '{wealthy.in}' where id;
     ```

## 5. Configure SMTP for booking e-mails

Use any provider that can send as `cabins@wealthy.in` (or another address on your domain):

| Provider | Host / port | Notes |
|---|---|---|
| Google Workspace SMTP relay | `smtp-relay.gmail.com:587` | Admin console → Apps → Gmail → Routing → SMTP relay |
| Microsoft 365 | `smtp.office365.com:587` | Needs SMTP AUTH enabled for the mailbox |
| Amazon SES / SendGrid / Postmark / Brevo | provider value, 587 | Verify the domain (SPF + DKIM) |

Add SPF/DKIM records so mail is not marked as spam. Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`.

## 6. Create the Render services

Option A — **Blueprint (recommended)**: Render dashboard → **New → Blueprint** → select the repository. Render reads `render.yaml` and creates:
* `wealthy-cabin-booking` — web service (Python), health check `/_stcore/health`
* `wealthy-cabin-notifications` — cron job every 5 minutes (`python -m scripts.process_notifications`)

Option B — manual web service: **New → Web Service**, runtime Python 3, region Singapore, plan Starter or above (the free plan sleeps and drops WebSocket sessions):
* Build command: `pip install -r requirements.txt`
* Start command: `streamlit run app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true`
* Health check path: `/_stcore/health`

Keep the web service at **one instance**: Streamlit sessions live in memory and Render does not use sticky sessions.

## 7. Environment variables on Render

Set on **both** services (Blueprint prompts for the `sync: false` ones):

| Variable | Value |
|---|---|
| `PYTHON_VERSION` | `3.12.8` |
| `SUPABASE_URL` | from step 2 |
| `SUPABASE_ANON_KEY` | from step 2 |
| `SUPABASE_SERVICE_ROLE_KEY` | from step 2 (secret) |
| `ALLOWED_EMAIL_DOMAIN` | `wealthy.in` |
| `APP_BASE_URL` | public URL of the web service |
| `OFFICE_TIMEZONE` | `Asia/Kolkata` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM` | from step 5 |
| `NOTIFICATIONS_ENABLED` | `true` |
| `APP_NAME` / `COMPANY_NAME` / `COMPANY_LOGO_URL` | optional branding fallbacks |

`DATABASE_URL` is **not** needed on Render.

## 8. Deploy

Push to `main` (auto-deploy) or click **Manual Deploy**. In the logs you should see `You can now view your Streamlit app`. Open the URL: the sign-in page shows "Wealthy Office Cabin Booking".

## 9. Create the first administrator securely

1. In the app, **Create account** with the admin's `@wealthy.in` address and enter the e-mailed 6-digit code. They will see "awaiting approval".
2. A Supabase project owner opens **SQL Editor** and runs (`database/scripts/bootstrap_first_admin.sql`):
   ```sql
   select private.bootstrap_first_admin('firstname.lastname@wealthy.in');
   ```
   It refuses if an admin already exists, the address is unverified or not on the domain. It is recorded in the audit log.
3. The admin clicks **Check again** (or signs in again) and now sees **Administration**.
4. Further admins: Administration → Employees → select an active employee → *Make administrator*.

## 10. Office time zone and working hours

Administration → **Settings**: time zone (default `Asia/Kolkata`), opening/closing time (placeholder 09:00–19:00), working days (placeholder Mon–Fri), minimum duration (15 min), maximum (≤ 120 min, enforced by the database), slot step, booking horizon, cancellation cut-off, reminder minutes, whether booker names are shown, and the access policy. Then pre-approve employees under **Employees → Pre-approved addresses** (or switch the policy to *Activate any verified company address*).

## 11. Check all 10 cabins

Administration → **Cabins**: set real names, locations, capacities and equipment for C01–C10. Then on **Availability** pick a time and confirm all ten cards appear, and book each cabin once (or check the timeline).

## 12. Acceptance tests in production

Do these with two employee accounts (A, B) and one admin:

| Test | Expected |
|---|---|
| Sign up with a Gmail/Yahoo/Outlook address | "Only official company e-mail addresses…" and no account in Supabase → Authentication → Users |
| Sign in before entering the code | "Please verify your e-mail address first" |
| Verified but not approved | "An administrator needs to approve your access"; no data visible |
| A books Cabin 01 10:00–11:00; B tries 10:30–11:30 | B gets "already booked" |
| B books Cabin 01 11:00–12:00 | succeeds (adjacent) |
| A and B click *Confirm* for the same slot at the same moment | exactly one succeeds |
| Book 2 h 15 min / 07:00 / yesterday | rejected with a clear message |
| B opens My bookings | sees only B's bookings |
| A cancels own booking | confirmation + e-mail; status *Cancelled* kept in history |
| Admin cancels B's booking | B gets the "cancelled by an administrator" e-mail |
| Admin disables B | B is blocked at the next page load; B's history remains |
| Admin › Audit log | shows each action above |
| Admin › Notifications | rows move from *pending* to *sent* within 5 minutes |

## 13. Logging and backups

* **Logs**: Render → service → *Logs* (stream to Datadog/Better Stack via Render log streams if required). The app logs errors without secrets; Supabase → *Logs* shows Auth and API logs.
* **Backups**: Supabase Pro and above take daily backups; enable **Point-in-Time Recovery** for minute-level restores. Also keep an independent nightly logical dump:
  ```bash
  pg_dump "$DATABASE_URL" --schema=public --schema=private --no-owner -Fc -f wealthy-cabins-$(date +%F).dump
  ```
  Store it encrypted outside Supabase (e.g. a private bucket with lifecycle rules). See `docs/SECURITY.md` for the restore drill.

## 14. HTTPS and a custom domain

Render serves HTTPS automatically. For `cabins.wealthy.in`: Render → service → *Settings → Custom Domains* → add the domain, create the CNAME record it shows at your DNS provider, wait for the certificate, then update `APP_BASE_URL` and Supabase *Site URL / Redirect URLs* to the new address and redeploy.
