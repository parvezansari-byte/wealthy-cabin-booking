-- =============================================================================
-- 0005_rls_and_grants.sql
-- Row Level Security and least-privilege grants.
--
-- How this interacts with Supabase Auth: the app calls PostgREST with the
-- signed-in user's JWT. PostgREST verifies the signature, switches to the
-- "authenticated" role and exposes the claims to auth.uid(). Every policy
-- below keys off auth.uid() plus private.is_active_employee()/is_admin(),
-- which re-read the profile and auth.users on every statement — so disabling
-- an employee or un-verifying an address takes effect immediately.
-- The "anon" role (no JWT) gets no table access at all.
-- =============================================================================

-- Start from zero: Supabase grants broad default privileges on public tables.
revoke all on public.office_settings, public.audit_logs, public.profiles, public.employee_allowlist,
              public.cabins, public.maintenance_blocks, public.bookings, public.notification_logs
  from anon, authenticated;
revoke all on sequence public.booking_ref_seq from anon, authenticated;

alter table public.office_settings     enable row level security;
alter table public.audit_logs          enable row level security;
alter table public.profiles            enable row level security;
alter table public.employee_allowlist  enable row level security;
alter table public.cabins              enable row level security;
alter table public.maintenance_blocks  enable row level security;
alter table public.bookings            enable row level security;
alter table public.notification_logs   enable row level security;

-- ---------------------------------------------------------------- office_settings
grant select on public.office_settings to authenticated;
grant update (app_name, company_name, logo_url, timezone, open_time, close_time, working_days,
              min_booking_minutes, max_booking_minutes, slot_minutes, booking_horizon_days,
              cancel_cutoff_minutes, reminder_minutes_before, show_booker_names, access_policy)
  on public.office_settings to authenticated;
-- allowed_email_domains is deliberately NOT updatable through the API.

drop policy if exists settings_read on public.office_settings;
create policy settings_read on public.office_settings
  for select to authenticated using (private.is_active_employee());

drop policy if exists settings_admin_update on public.office_settings;
create policy settings_admin_update on public.office_settings
  for update to authenticated using (private.is_admin()) with check (private.is_admin());

drop trigger if exists office_settings_audit on public.office_settings;
create trigger office_settings_audit after update on public.office_settings
  for each row execute function private.audit_row_change();

-- ---------------------------------------------------------------- audit_logs
grant select on public.audit_logs to authenticated;
drop policy if exists audit_admin_read on public.audit_logs;
create policy audit_admin_read on public.audit_logs
  for select to authenticated using (private.is_admin());

-- ---------------------------------------------------------------- profiles
grant select on public.profiles to authenticated;
grant update (full_name, department) on public.profiles to authenticated;

drop policy if exists profiles_read_own on public.profiles;
create policy profiles_read_own on public.profiles
  for select to authenticated using (id = auth.uid());

drop policy if exists profiles_admin_read on public.profiles;
create policy profiles_admin_read on public.profiles
  for select to authenticated using (private.is_admin());

drop policy if exists profiles_update_own on public.profiles;
create policy profiles_update_own on public.profiles
  for update to authenticated
  using (id = auth.uid() and private.is_active_employee())
  with check (id = auth.uid());

-- ---------------------------------------------------------------- employee_allowlist
grant select, insert, delete on public.employee_allowlist to authenticated;
grant update (note) on public.employee_allowlist to authenticated;

drop policy if exists allowlist_admin_all on public.employee_allowlist;
create policy allowlist_admin_all on public.employee_allowlist
  for all to authenticated using (private.is_admin()) with check (private.is_admin());

-- ---------------------------------------------------------------- cabins
grant select on public.cabins to authenticated;
grant update (name, location, capacity, equipment, is_active, sort_order) on public.cabins to authenticated;

drop policy if exists cabins_read on public.cabins;
create policy cabins_read on public.cabins
  for select to authenticated using (private.is_active_employee());

drop policy if exists cabins_admin_update on public.cabins;
create policy cabins_admin_update on public.cabins
  for update to authenticated using (private.is_admin()) with check (private.is_admin());

-- ---------------------------------------------------------------- maintenance_blocks
-- Writes only via admin_create_maintenance / admin_remove_maintenance.
grant select on public.maintenance_blocks to authenticated;
drop policy if exists maintenance_read on public.maintenance_blocks;
create policy maintenance_read on public.maintenance_blocks
  for select to authenticated using (private.is_active_employee());

-- ---------------------------------------------------------------- bookings
-- Read: own rows, or everything for admins. Other people's bookings are only
-- visible as anonymous busy intervals through get_availability().
-- Writes: only via book_cabin / cancel_booking / admin functions.
grant select on public.bookings to authenticated;

drop policy if exists bookings_read_own on public.bookings;
create policy bookings_read_own on public.bookings
  for select to authenticated using (user_id = auth.uid() and private.is_active_employee());

drop policy if exists bookings_admin_read on public.bookings;
create policy bookings_admin_read on public.bookings
  for select to authenticated using (private.is_admin());

-- ---------------------------------------------------------------- notification_logs
grant select on public.notification_logs to authenticated;
drop policy if exists notifications_admin_read on public.notification_logs;
create policy notifications_admin_read on public.notification_logs
  for select to authenticated using (private.is_admin());

-- ---------------------------------------------------------------- service_role
-- Used only by the server-side notification worker. It bypasses RLS by design.
grant select, insert, update on public.notification_logs to service_role;
grant select on public.bookings, public.cabins, public.profiles, public.office_settings to service_role;

-- ---------------------------------------------------------------- functions
revoke execute on all functions in schema public  from public, anon;
revoke execute on all functions in schema private from public, anon;

grant execute on function public.get_public_branding()                         to anon, authenticated;
grant execute on function public.get_my_access()                               to authenticated;
grant execute on function public.book_cabin(uuid, timestamptz, timestamptz, text, integer, text) to authenticated;
grant execute on function public.cancel_booking(uuid, text)                    to authenticated;
grant execute on function public.get_availability(timestamptz, timestamptz)    to authenticated;
grant execute on function public.admin_set_employee_status(uuid, text, boolean) to authenticated;
grant execute on function public.admin_set_role(uuid, text)                    to authenticated;
grant execute on function public.admin_create_maintenance(uuid, timestamptz, timestamptz, text, boolean) to authenticated;
grant execute on function public.admin_remove_maintenance(uuid)                to authenticated;
grant execute on function public.admin_retry_failed_notifications()            to authenticated;

grant execute on function public.claim_notifications(integer)                  to service_role;
grant execute on function public.complete_notification(uuid, text, text)       to service_role;
grant execute on function public.queue_due_reminders()                         to service_role;

-- Helpers referenced from policies must be executable by the caller's role.
grant execute on function private.is_active_employee(), private.is_admin(),
                         private.is_allowed_email(text), private.email_domain(text),
                         private.settings()
  to authenticated, service_role;
-- private.bootstrap_first_admin(text) is intentionally granted to nobody.
