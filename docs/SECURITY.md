# Security checklist, backup and recovery

## Pre-go-live checklist

**Secrets and configuration**
- [ ] `.env` is not in git; `git log -p | grep -i service_role` finds nothing
- [ ] `SUPABASE_SERVICE_ROLE_KEY`, `SMTP_PASSWORD` exist only in Render environment variables
- [ ] `ALLOWED_EMAIL_DOMAIN=wealthy.in` on Render and `office_settings.allowed_email_domains = {wealthy.in}` in the database
- [ ] `APP_ENV=production`; `.streamlit/config.toml` has `showErrorDetails = "none"` and `enableXsrfProtection = true`
- [ ] GitHub repository is private; branch protection on `main` requires the CI workflow to pass

**Supabase Auth**
- [ ] *Confirm email* is ON; minimum password length ≥ 10
- [ ] Custom SMTP configured (not the built-in mailer)
- [ ] Site URL / Redirect URLs list only the production URL
- [ ] Auth rate limits reviewed; CAPTCHA enabled if exposed publicly

**Database**
- [ ] All six migrations applied; `select tablename, rowsecurity from pg_tables where schemaname='public'` → every row `true`
- [ ] `select private.bootstrap_first_admin(...)` run once; exactly the intended people have `role = 'admin'`
- [ ] Access policy chosen (`approval` recommended) and allowlist populated
- [ ] Nobody uses the service-role key from a laptop for day-to-day work
- [ ] Do not delete users in Supabase → Authentication → Users: disable them in the app instead (deletion is deliberately blocked to keep booking history)

**Operations**
- [ ] Render web service runs a single instance on a paid plan; health check `/_stcore/health`
- [ ] Notification cron job deployed and succeeding; Admin › Notifications shows `sent`
- [ ] Backups / PITR enabled and a restore drill done (below)
- [ ] Off-boarding procedure: HR → admin disables the employee (optionally cancelling future bookings) on their last day

## Controls implemented

| Threat | Control | Where |
|---|---|---|
| Personal or look-alike e-mail domains | exact domain match in DB trigger on `auth.users`, in `is_active_employee()`, and in the app | `0002`, `core/security.py` |
| Unverified mailbox | `email_confirmed_at` required by `is_active_employee()`; Supabase *Confirm email* | `0002` |
| Verified address but not an employee | `approval` access policy + allowlist | `0002`, Admin › Employees |
| Self-promotion to admin | no column grant on `role`/`status`, guard trigger, admin functions refuse self-changes, first admin only via SQL editor | `0002`, `0004`, `0005` |
| Reading others' bookings | RLS: own rows only; availability function hides details | `0005`, `get_availability` |
| Tampering with bookings via API | no INSERT/UPDATE/DELETE grants on `bookings`; writes only through checked functions | `0005` |
| Double booking (incl. races) | exclusion constraint + per-cabin advisory lock | `0003`, `0004` |
| > 2 h bookings | `CHECK (end_at - start_at <= 120 min)` + settings `CHECK (max ≤ 120)` + function check | `0001`, `0003`, `0004` |
| SQL injection | parameterised PostgREST calls only | `core/database.py` |
| XSS | Streamlit escaping + `esc()` for custom HTML + https-only logo URL | `ui/components.py` |
| CSRF | Streamlit XSRF protection; no cookie-authenticated form posts | `.streamlit/config.toml` |
| Credential stuffing | Supabase Auth limits + in-app limiter | `core/ratelimit.py` |
| Account enumeration | identical responses for existing/non-existing accounts on sign-up and reset | `core/auth.py` |
| Session theft | tokens only in server memory; idle timeout; no tokens in URLs or cookies | `ui/context.py` |
| Information leakage in errors | error-code mapping; generic fallback; no tracebacks to users | `core/errors.py` |
| CSV formula injection | `csv_safe()` on every exported cell | `core/reports.py` |
| Notes exposure | notes visible to owner/admin only; redacted in audit log; excluded from exports by default | `0002`, `0003` |
| Lost/duplicated e-mails | transactional outbox, unique per event, leases, back-off, admin retry | `0003`, `0004`, `core/notifications.py` |
| Repudiation | append-only audit log of bookings, cancellations, settings, cabins, roles, access, allowlist | `0002` |

## Backup and recovery

**What to protect**: the Supabase database (`public`, `private` and `auth` schemas). The app itself is stateless and is rebuilt from git.

**Backups**
1. Supabase managed daily backups (Pro plan and above); enable **Point-in-Time Recovery** for a recovery point objective of minutes.
2. Independent nightly logical dump of `public` and `private` (see DEPLOYMENT step 13), kept encrypted for 30 days outside Supabase.

**Restore drill (quarterly)**
1. Create a scratch Supabase project.
2. Restore the latest backup (Dashboard → Database → Backups → Restore to new project), or `pg_restore --no-owner -d "$SCRATCH_DATABASE_URL" wealthy-cabins-<date>.dump` after applying `0001`–`0005` to the scratch project.
3. Point a local copy of the app at the scratch project and sign in as a test admin; check bookings, audit log and settings.
4. Record the time taken (recovery time objective target: under 2 hours).

**Incident playbooks**
* *Service-role key leaked*: Supabase → API → roll the secret key; update Render env vars; redeploy both services; review `audit_logs` and Supabase API logs for the exposure window.
* *Compromised employee account*: Admin › Employees → Disable (and cancel future bookings if appropriate); in Supabase → Authentication → Users, sign the user out of all sessions; ask them to reset their password.
* *Accidental mass cancellation*: cancellations are soft (`status = 'cancelled'` with who/when/why) — identify rows in `audit_logs` and recreate bookings via the app, or restore with PITR to a point before the change.
