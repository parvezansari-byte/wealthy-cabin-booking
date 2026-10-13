-- ALL MIGRATIONS (0001-0006) in one file, for pasting into the Supabase SQL Editor
-- (Primary database mode, NOT Logs). Safe to run more than once.
-- Creates everything in the 'cabins' and 'cabins_private' schemas only.

-- ===================== migrations/0001_foundation.sql =====================
-- =============================================================================
-- 0001_foundation.sql
-- Extensions, the private helper schema, office settings and generic triggers.
-- Run as the database owner (the "postgres" role in Supabase).
-- =============================================================================

create schema if not exists extensions;
create extension if not exists btree_gist with schema extensions;

-- Everything this app creates lives in two schemas of its own, so it can share
-- a Supabase project with other apps without touching their objects:
--   cabins          tables + API functions (add "cabins" to Supabase's Exposed schemas)
--   cabins_private  internal helpers (never exposed through the API)
-- Nothing is created in, or changed in, the "public" schema.
create schema if not exists cabins;
grant usage on schema cabins to anon, authenticated, service_role;

create schema if not exists cabins_private;
revoke all on schema cabins_private from public;
grant usage on schema cabins_private to authenticated, service_role;

-- Functions in our schemas are not executable by PUBLIC unless granted explicitly.
alter default privileges in schema cabins_private revoke execute on functions from public;
alter default privileges in schema cabins          revoke execute on functions from public;

-- -----------------------------------------------------------------------------
-- updated_at trigger
-- -----------------------------------------------------------------------------
create or replace function cabins_private.touch_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

-- -----------------------------------------------------------------------------
-- Office settings (exactly one row)
-- -----------------------------------------------------------------------------
create table if not exists cabins.office_settings (
  id                        boolean primary key default true check (id),
  app_name                  text not null default 'Wealthy Office Cabin Booking'
                              check (char_length(app_name) between 3 and 80),
  company_name              text not null default 'Wealthy'
                              check (char_length(company_name) between 1 and 80),
  logo_url                  text check (logo_url is null or logo_url ~ '^https://[^\s<>"'']+$'),
  timezone                  text not null default 'Asia/Kolkata',
  open_time                 time not null default '09:00',
  close_time                time not null default '19:00',
  working_days              smallint[] not null default '{1,2,3,4,5}',   -- ISO: 1=Mon … 7=Sun
  min_booking_minutes       integer not null default 15 check (min_booking_minutes between 5 and 120),
  max_booking_minutes       integer not null default 120 check (max_booking_minutes between 15 and 120),
  slot_minutes              integer not null default 15 check (slot_minutes in (5, 10, 15, 30)),
  booking_horizon_days      integer not null default 30 check (booking_horizon_days between 1 and 365),
  cancel_cutoff_minutes     integer not null default 0 check (cancel_cutoff_minutes between 0 and 1440),
  reminder_minutes_before   integer not null default 15 check (reminder_minutes_before between 0 and 1440),
  show_booker_names         boolean not null default false,
  -- 'approval': verified @domain users stay pending until an admin approves them
  --             or their address is on employee_allowlist.
  -- 'domain'  : any verified address on an allowed domain is activated.
  access_policy             text not null default 'approval' check (access_policy in ('approval', 'domain')),
  -- Only changeable by the database owner (no column grant to authenticated).
  allowed_email_domains     text[] not null default '{wealthy.in}',
  updated_at                timestamptz not null default now(),
  updated_by                uuid,
  constraint office_hours_order check (close_time > open_time),
  constraint booking_minutes_order check (min_booking_minutes <= max_booking_minutes),
  constraint working_days_valid check (
    cardinality(working_days) between 1 and 7
    and working_days <@ '{1,2,3,4,5,6,7}'::smallint[]
  ),
  constraint allowed_domains_present check (cardinality(allowed_email_domains) >= 1)
);

create or replace function cabins_private.validate_office_settings()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  d text;
begin
  if not exists (select 1 from pg_catalog.pg_timezone_names where name = new.timezone) then
    raise exception 'INVALID_TIMEZONE' using detail = 'Unknown time zone: ' || new.timezone;
  end if;
  foreach d in array new.allowed_email_domains loop
    if d is null or d <> lower(d) or d !~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$' then
      raise exception 'INVALID_EMAIL_DOMAIN' using detail = 'Domains must be lowercase host names without "@".';
    end if;
  end loop;
  new.updated_by := auth.uid();
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists office_settings_validate on cabins.office_settings;
create trigger office_settings_validate
  before insert or update on cabins.office_settings
  for each row execute function cabins_private.validate_office_settings();

insert into cabins.office_settings (id) values (true) on conflict (id) do nothing;

create or replace function cabins_private.settings()
returns cabins.office_settings
language sql
stable
security definer
set search_path = ''
as $$
  select s from cabins.office_settings s where s.id
$$;

-- -----------------------------------------------------------------------------
-- E-mail helpers
-- -----------------------------------------------------------------------------
create or replace function cabins_private.email_domain(p_email text)
returns text
language sql
immutable
set search_path = ''
as $$
  select lower(substring(btrim(p_email) from '@([^@]+)$'))
$$;

-- Exact (not suffix) match against the configured domains, so
-- "x@evilwealthy.in" and "x@wealthy.in.example.com" are rejected.
create or replace function cabins_private.is_allowed_email(p_email text)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select coalesce(
    p_email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'
    and cabins_private.email_domain(p_email) = any ((cabins_private.settings()).allowed_email_domains),
    false)
$$;

grant execute on function cabins_private.email_domain(text)      to authenticated, service_role;
grant execute on function cabins_private.is_allowed_email(text)  to authenticated, service_role;
grant execute on function cabins_private.settings()              to authenticated, service_role;

-- ===================== migrations/0002_identity_and_audit.sql =====================
-- =============================================================================
-- 0002_identity_and_audit.sql
-- Audit log, employee profiles, allowlist, auth.users triggers and the
-- authorization helpers used by every RLS policy.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Audit log (append-only)
-- -----------------------------------------------------------------------------
create table if not exists cabins.audit_logs (
  id           bigint generated always as identity primary key,
  occurred_at  timestamptz not null default now(),
  actor_id     uuid,                       -- null = system / database owner
  actor_email  text,
  action       text not null,
  entity_type  text not null,
  entity_id    text,
  details      jsonb not null default '{}'::jsonb
);
create index if not exists audit_logs_occurred_at_idx on cabins.audit_logs (occurred_at desc);
create index if not exists audit_logs_entity_idx on cabins.audit_logs (entity_type, entity_id);

create or replace function cabins_private.block_audit_mutation()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception 'AUDIT_LOG_IMMUTABLE' using detail = 'Audit log entries cannot be changed or deleted.';
end;
$$;

drop trigger if exists audit_logs_immutable on cabins.audit_logs;
create trigger audit_logs_immutable
  before update or delete on cabins.audit_logs
  for each row execute function cabins_private.block_audit_mutation();

create or replace function cabins_private.write_audit(
  p_action text, p_entity_type text, p_entity_id text, p_details jsonb default '{}'::jsonb)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid uuid := auth.uid();
begin
  insert into cabins.audit_logs (actor_id, actor_email, action, entity_type, entity_id, details)
  values (v_uid,
          (select u.email from auth.users u where u.id = v_uid),
          p_action, p_entity_type, p_entity_id, coalesce(p_details, '{}'::jsonb));
end;
$$;

-- Generic row-change audit trigger. Column names in TG_ARGV are redacted
-- (their values are never copied into the log; only the fact they changed).
create or replace function cabins_private.audit_row_change()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_old jsonb := case when tg_op in ('UPDATE', 'DELETE') then to_jsonb(old) end;
  v_new jsonb := case when tg_op in ('INSERT', 'UPDATE') then to_jsonb(new) end;
  v_redact text[] := coalesce(tg_argv::text[], '{}');
  v_changes jsonb := '{}'::jsonb;
  v_key text;
  v_entity_id text;
  v_action text := tg_table_name || '.' || lower(tg_op);
begin
  v_entity_id := coalesce(v_new ->> 'id', v_old ->> 'id', v_new ->> 'email', v_old ->> 'email');

  if tg_op = 'UPDATE' then
    for v_key in select jsonb_object_keys(v_new) loop
      continue when v_key in ('updated_at', 'updated_by');
      if (v_new -> v_key) is distinct from (v_old -> v_key) then
        if v_key = any (v_redact) then
          v_changes := v_changes || jsonb_build_object(v_key, jsonb_build_object('changed', true));
        else
          v_changes := v_changes || jsonb_build_object(v_key,
                         jsonb_build_object('old', v_old -> v_key, 'new', v_new -> v_key));
        end if;
      end if;
    end loop;
    if v_changes = '{}'::jsonb then
      return new;
    end if;
    -- Friendlier action names for the events admins look for most.
    if tg_table_name = 'bookings' and v_changes ? 'status' and v_new ->> 'status' = 'cancelled' then
      v_action := 'booking.cancelled';
    elsif tg_table_name = 'profiles' and v_changes ? 'role' then
      v_action := 'profile.role_changed';
    elsif tg_table_name = 'profiles' and v_changes ? 'status' then
      v_action := 'profile.status_changed';
    end if;
  else
    v_changes := coalesce(v_new, v_old) - v_redact;
    if tg_table_name = 'bookings' and tg_op = 'INSERT' then
      v_action := 'booking.created';
    end if;
  end if;

  perform cabins_private.write_audit(v_action, tg_table_name, v_entity_id, v_changes);
  return coalesce(new, old);
end;
$$;

-- -----------------------------------------------------------------------------
-- Profiles
-- -----------------------------------------------------------------------------
create table if not exists cabins.profiles (
  id           uuid primary key references auth.users (id) on delete restrict,
  email        text not null unique check (email = lower(email)),
  full_name    text not null default '' check (char_length(full_name) <= 120),
  department   text check (department is null or char_length(department) <= 80),
  role         text not null default 'employee' check (role in ('employee', 'admin')),
  status       text not null default 'pending' check (status in ('pending', 'active', 'disabled')),
  approved_at  timestamptz,
  approved_by  uuid references cabins.profiles (id),
  disabled_at  timestamptz,
  disabled_by  uuid references cabins.profiles (id),
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create index if not exists profiles_status_idx on cabins.profiles (status);

drop trigger if exists profiles_touch on cabins.profiles;
create trigger profiles_touch before update on cabins.profiles
  for each row execute function cabins_private.touch_updated_at();

-- Defence in depth: role/status can never be changed by an API caller directly,
-- only by SECURITY DEFINER functions (which run as the table owner).
create or replace function cabins_private.guard_profile_privileged_columns()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if current_user in ('anon', 'authenticated')
     and (new.role is distinct from old.role
          or new.status is distinct from old.status
          or new.email is distinct from old.email
          or new.id is distinct from old.id) then
    raise exception 'FORBIDDEN' using errcode = '42501',
      detail = 'Role, status and e-mail can only be changed through authorised administration.';
  end if;
  return new;
end;
$$;

drop trigger if exists profiles_guard on cabins.profiles;
create trigger profiles_guard before update on cabins.profiles
  for each row execute function cabins_private.guard_profile_privileged_columns();

drop trigger if exists profiles_audit on cabins.profiles;
create trigger profiles_audit after insert or update on cabins.profiles
  for each row execute function cabins_private.audit_row_change();

-- -----------------------------------------------------------------------------
-- Allowlist of pre-approved employee addresses (used by access_policy='approval')
-- -----------------------------------------------------------------------------
create table if not exists cabins.employee_allowlist (
  email       text primary key check (email = lower(btrim(email))),
  note        text check (note is null or char_length(note) <= 200),
  added_by    uuid references cabins.profiles (id),
  created_at  timestamptz not null default now()
);

create or replace function cabins_private.validate_allowlist_entry()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  new.email := lower(btrim(new.email));
  if not cabins_private.is_allowed_email(new.email) then
    raise exception 'EMAIL_DOMAIN_NOT_ALLOWED'
      using detail = 'Only addresses on the company domain can be allowlisted.';
  end if;
  new.added_by := coalesce(new.added_by, auth.uid());
  return new;
end;
$$;

drop trigger if exists employee_allowlist_validate on cabins.employee_allowlist;
create trigger employee_allowlist_validate before insert or update on cabins.employee_allowlist
  for each row execute function cabins_private.validate_allowlist_entry();

drop trigger if exists employee_allowlist_audit on cabins.employee_allowlist;
create trigger employee_allowlist_audit after insert or update or delete on cabins.employee_allowlist
  for each row execute function cabins_private.audit_row_change();

-- -----------------------------------------------------------------------------
-- Activation logic shared by the auth.users triggers and the allowlist.
-- A profile becomes 'active' only when ALL of these hold:
--   * the auth user has a confirmed e-mail (ownership verified by Supabase Auth)
--   * the address is on an allowed company domain
--   * policy = 'domain', or the address is on employee_allowlist
-- Disabled profiles are never re-activated automatically.
-- -----------------------------------------------------------------------------
create or replace function cabins_private.sync_profile_activation(p_user_id uuid)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_user  auth.users%rowtype;
  v_email text;
begin
  select * into v_user from auth.users where id = p_user_id;
  if not found then
    return;
  end if;
  v_email := lower(btrim(v_user.email));

  update cabins.profiles set email = v_email
   where id = p_user_id and email is distinct from v_email;

  if v_user.email_confirmed_at is not null
     and cabins_private.is_allowed_email(v_email)
     and ((cabins_private.settings()).access_policy = 'domain'
          or exists (select 1 from cabins.employee_allowlist a where a.email = v_email)) then
    update cabins.profiles
       set status = 'active', approved_at = now(), approved_by = null
     where id = p_user_id and status = 'pending';
  end if;
end;
$$;

-- -----------------------------------------------------------------------------
-- auth.users hooks.
-- This Supabase project may be shared with other apps, so these hooks NEVER block
-- or fail a sign-up: non-company users simply get no cabin profile (and therefore
-- no access). Company-domain users get a 'pending' profile that becomes active
-- per the access policy. Any unexpected error is downgraded to a warning so the
-- other app's authentication can never be broken by this one.
-- -----------------------------------------------------------------------------
create or replace function cabins_private.ensure_profile(p_user_id uuid)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_user auth.users%rowtype;
begin
  select * into v_user from auth.users where id = p_user_id;
  if not found then
    return;
  end if;
  if cabins_private.is_allowed_email(v_user.email) then
    insert into cabins.profiles (id, email, full_name)
    values (v_user.id, lower(btrim(v_user.email)),
            left(btrim(coalesce(v_user.raw_user_meta_data ->> 'full_name', '')), 120))
    on conflict (id) do nothing;
  end if;
  perform cabins_private.sync_profile_activation(p_user_id);
end;
$$;

create or replace function cabins_private.handle_auth_user_change()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  begin
    perform cabins_private.ensure_profile(new.id);
  exception when others then
    raise warning 'cabins: could not sync profile for user %: %', new.id, sqlerrm;
  end;
  return new;
end;
$$;

-- Remove the project-wide sign-up block used by earlier single-app versions.
drop trigger if exists wealthy_enforce_email_domain on auth.users;
drop function if exists cabins_private.enforce_auth_email_domain();

drop trigger if exists wealthy_on_auth_user_created on auth.users;
create trigger wealthy_on_auth_user_created
  after insert on auth.users
  for each row execute function cabins_private.handle_auth_user_change();

drop trigger if exists wealthy_on_auth_user_updated on auth.users;
create trigger wealthy_on_auth_user_updated
  after update of email, email_confirmed_at on auth.users
  for each row execute function cabins_private.handle_auth_user_change();

-- When an admin allowlists an address whose owner already verified it, activate them.
create or replace function cabins_private.handle_allowlist_insert()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  perform cabins_private.sync_profile_activation(p.id)
     from cabins.profiles p where p.email = new.email;
  return new;
end;
$$;

drop trigger if exists employee_allowlist_activate on cabins.employee_allowlist;
create trigger employee_allowlist_activate after insert on cabins.employee_allowlist
  for each row execute function cabins_private.handle_allowlist_insert();

-- -----------------------------------------------------------------------------
-- Authorization helpers used by RLS policies and RPCs
-- -----------------------------------------------------------------------------
create or replace function cabins_private.is_active_employee()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
      from cabins.profiles p
      join auth.users u on u.id = p.id
     where p.id = auth.uid()
       and p.status = 'active'
       and u.email_confirmed_at is not null
       and u.deleted_at is null
       and (u.banned_until is null or u.banned_until < now())
       and lower(u.email) = p.email
       and cabins_private.is_allowed_email(u.email)
  )
$$;

create or replace function cabins_private.is_admin()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select cabins_private.is_active_employee()
     and exists (select 1 from cabins.profiles p where p.id = auth.uid() and p.role = 'admin')
$$;

grant execute on function cabins_private.is_active_employee() to authenticated, service_role;
grant execute on function cabins_private.is_admin()           to authenticated, service_role;

-- Company users who signed up before this app was installed get a profile now.
select cabins_private.ensure_profile(u.id)
  from auth.users u
 where cabins_private.is_allowed_email(u.email);

-- ===================== migrations/0003_cabins_and_bookings.sql =====================
-- =============================================================================
-- 0003_cabins_and_bookings.sql
-- Cabins, maintenance blocks, bookings (with the overlap exclusion constraint)
-- and the notification outbox.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Cabins
-- -----------------------------------------------------------------------------
create table if not exists cabins.cabins (
  id          uuid primary key default gen_random_uuid(),
  code        text not null unique check (code ~ '^C[0-9]{2}$'),
  name        text not null check (char_length(btrim(name)) between 1 and 60),
  location    text check (location is null or char_length(location) <= 120),
  capacity    integer not null check (capacity between 1 and 100),
  equipment   text[] not null default '{}'
                check (cardinality(equipment) <= 12),
  is_active   boolean not null default true,
  sort_order  integer not null default 0,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

drop trigger if exists cabins_touch on cabins.cabins;
create trigger cabins_touch before update on cabins.cabins
  for each row execute function cabins_private.touch_updated_at();

drop trigger if exists cabins_audit on cabins.cabins;
create trigger cabins_audit after insert or update on cabins.cabins
  for each row execute function cabins_private.audit_row_change();

-- -----------------------------------------------------------------------------
-- Maintenance blocks
-- -----------------------------------------------------------------------------
create table if not exists cabins.maintenance_blocks (
  id          uuid primary key default gen_random_uuid(),
  cabin_id    uuid not null references cabins.cabins (id) on delete restrict,
  start_at    timestamptz not null,
  end_at      timestamptz not null,
  reason      text not null check (char_length(btrim(reason)) between 3 and 200),
  status      text not null default 'active' check (status in ('active', 'removed')),
  created_by  uuid references cabins.profiles (id),
  removed_by  uuid references cabins.profiles (id),
  removed_at  timestamptz,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  constraint maintenance_time_order check (end_at > start_at),
  constraint maintenance_max_length check (end_at - start_at <= interval '90 days')
);
create index if not exists maintenance_cabin_time_idx
  on cabins.maintenance_blocks using gist (cabin_id, tstzrange(start_at, end_at, '[)'))
  where status = 'active';

drop trigger if exists maintenance_touch on cabins.maintenance_blocks;
create trigger maintenance_touch before update on cabins.maintenance_blocks
  for each row execute function cabins_private.touch_updated_at();

drop trigger if exists maintenance_audit on cabins.maintenance_blocks;
create trigger maintenance_audit after insert or update on cabins.maintenance_blocks
  for each row execute function cabins_private.audit_row_change();

-- -----------------------------------------------------------------------------
-- Bookings
-- Stored instants are timestamptz (UTC internally); the office time zone is
-- applied only for validation and display. Intervals are half-open [start, end).
-- -----------------------------------------------------------------------------
create sequence if not exists cabins.booking_ref_seq start with 100001;

create table if not exists cabins.bookings (
  id             uuid primary key default gen_random_uuid(),
  reference      text not null unique
                   default ('WCB-' || lpad(nextval('cabins.booking_ref_seq')::text, 6, '0')),
  cabin_id       uuid not null references cabins.cabins (id) on delete restrict,
  user_id        uuid not null references cabins.profiles (id) on delete restrict,
  start_at       timestamptz not null,
  end_at         timestamptz not null,
  title          text not null check (char_length(btrim(title)) between 3 and 120),
  attendees      integer not null default 1 check (attendees between 1 and 100),
  notes          text check (notes is null or char_length(notes) <= 1000),
  status         text not null default 'confirmed' check (status in ('confirmed', 'cancelled')),
  cancelled_at   timestamptz,
  cancelled_by   uuid references cabins.profiles (id),
  cancel_reason  text check (cancel_reason is null or char_length(cancel_reason) <= 300),
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  constraint bookings_time_order   check (end_at > start_at),
  -- Hard ceiling, independent of the configurable setting.
  constraint bookings_max_duration check (end_at - start_at <= interval '120 minutes'),
  constraint bookings_cancel_state check ((status = 'cancelled') = (cancelled_at is not null)),
  -- The database itself refuses overlapping confirmed bookings for one cabin.
  -- '[)' makes back-to-back bookings (10:00-11:00 and 11:00-12:00) legal.
  constraint bookings_no_overlap exclude using gist (
    cabin_id with =,
    tstzrange(start_at, end_at, '[)') with &&
  ) where (status = 'confirmed')
);

create index if not exists bookings_user_start_idx  on cabins.bookings (user_id, start_at desc);
create index if not exists bookings_start_idx       on cabins.bookings (start_at);
create index if not exists bookings_cabin_start_idx on cabins.bookings (cabin_id, start_at);
create index if not exists bookings_status_start_idx on cabins.bookings (status, start_at);

drop trigger if exists bookings_touch on cabins.bookings;
create trigger bookings_touch before update on cabins.bookings
  for each row execute function cabins_private.touch_updated_at();

-- Meeting notes are redacted from the audit log.
drop trigger if exists bookings_audit on cabins.bookings;
create trigger bookings_audit after insert or update on cabins.bookings
  for each row execute function cabins_private.audit_row_change('notes');

-- -----------------------------------------------------------------------------
-- Notification outbox / delivery log
-- Rows are written in the SAME transaction as the booking change, then sent
-- after commit. A failed e-mail never rolls back or duplicates a booking.
-- -----------------------------------------------------------------------------
create table if not exists cabins.notification_logs (
  id               uuid primary key default gen_random_uuid(),
  booking_id       uuid not null references cabins.bookings (id) on delete restrict,
  kind             text not null check (kind in (
                     'booking_confirmed', 'booking_cancelled', 'booking_cancelled_by_admin',
                     'maintenance_cancellation', 'reminder')),
  recipient_email  text not null,
  status           text not null default 'pending'
                     check (status in ('pending', 'sending', 'sent', 'failed', 'skipped')),
  attempts         integer not null default 0,
  last_error       text,
  next_attempt_at  timestamptz not null default now(),
  locked_at        timestamptz,
  sent_at          timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  constraint notification_once_per_event unique (booking_id, kind)
);
create index if not exists notification_due_idx
  on cabins.notification_logs (next_attempt_at) where status in ('pending', 'failed', 'sending');

drop trigger if exists notification_touch on cabins.notification_logs;
create trigger notification_touch before update on cabins.notification_logs
  for each row execute function cabins_private.touch_updated_at();

-- ===================== migrations/0004_booking_and_admin_functions.sql =====================
-- =============================================================================
-- 0004_booking_and_admin_functions.sql
-- Every write to bookings, maintenance, roles and access goes through these
-- SECURITY DEFINER functions. Each one re-derives the caller from the verified
-- JWT (auth.uid()) and re-checks authorisation; nothing is trusted from the UI.
--
-- Business errors are raised with SQLSTATE P0001 and a stable machine-readable
-- MESSAGE (e.g. CABIN_ALREADY_BOOKED); the DETAIL carries human-readable text.
-- =============================================================================

create or replace function cabins_private.fail(p_code text, p_detail text, p_sqlstate text default 'P0001')
returns void
language plpgsql
set search_path = ''
as $$
begin
  raise exception using message = p_code, detail = p_detail, errcode = p_sqlstate;
end;
$$;

create or replace function cabins_private.require_employee()
returns uuid
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_uid uuid := auth.uid();
begin
  if v_uid is null then
    perform cabins_private.fail('AUTH_REQUIRED', 'Please sign in to continue.', '42501');
  end if;
  if not cabins_private.is_active_employee() then
    perform cabins_private.fail('EMPLOYEE_NOT_AUTHORIZED',
      'Your account is not an active, verified employee account.', '42501');
  end if;
  return v_uid;
end;
$$;

create or replace function cabins_private.require_admin()
returns uuid
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_uid uuid := cabins_private.require_employee();
begin
  if not cabins_private.is_admin() then
    perform cabins_private.fail('ADMIN_REQUIRED', 'Administrator access is required.', '42501');
  end if;
  return v_uid;
end;
$$;

create or replace function cabins_private.lock_cabin(p_cabin_id uuid)
returns void
language sql
set search_path = ''
as $$
  select pg_advisory_xact_lock(hashtextextended('wealthy.cabin:' || p_cabin_id::text, 0))
$$;

create or replace function cabins_private.queue_notification(p_booking_id uuid, p_kind text)
returns void
language sql
security definer
set search_path = ''
as $$
  insert into cabins.notification_logs (booking_id, kind, recipient_email)
  select b.id, p_kind, p.email
    from cabins.bookings b join cabins.profiles p on p.id = b.user_id
   where b.id = p_booking_id
  on conflict (booking_id, kind) do nothing
$$;

-- -----------------------------------------------------------------------------
-- Caller's own access state (works for pending users too, so the UI can explain)
-- -----------------------------------------------------------------------------
create or replace function cabins.get_my_access()
returns jsonb
language sql
stable
security definer
set search_path = ''
as $$
  select jsonb_build_object(
    'user_id',        p.id,
    'email',          p.email,
    'full_name',      p.full_name,
    'department',     p.department,
    'role',           p.role,
    'status',         p.status,
    'email_verified', u.email_confirmed_at is not null,
    'domain_allowed', cabins_private.is_allowed_email(u.email),
    'is_employee',    cabins_private.is_active_employee(),
    'is_admin',       cabins_private.is_admin())
    from cabins.profiles p
    join auth.users u on u.id = p.id
   where p.id = auth.uid()
$$;

-- Branding shown on the sign-in page (no personal data).
create or replace function cabins.get_public_branding()
returns jsonb
language sql
stable
security definer
set search_path = ''
as $$
  select jsonb_build_object('app_name', s.app_name, 'company_name', s.company_name, 'logo_url', s.logo_url)
    from cabins.office_settings s where s.id
$$;

-- -----------------------------------------------------------------------------
-- Booking
-- -----------------------------------------------------------------------------
create or replace function cabins.book_cabin(
  p_cabin_id   uuid,
  p_start_at   timestamptz,
  p_end_at     timestamptz,
  p_title      text,
  p_attendees  integer default 1,
  p_notes      text default null)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid         uuid := cabins_private.require_employee();
  s             cabins.office_settings := cabins_private.settings();
  v_cabin       cabins.cabins;
  v_booking     cabins.bookings;
  v_title       text := btrim(coalesce(p_title, ''));
  v_notes       text := nullif(btrim(coalesce(p_notes, '')), '');
  v_minutes     numeric;
  v_local_start timestamp;
  v_local_end   timestamp;
begin
  if p_cabin_id is null or p_start_at is null or p_end_at is null then
    perform cabins_private.fail('MISSING_FIELDS', 'Cabin, start time and end time are required.');
  end if;
  if char_length(v_title) < 3 or char_length(v_title) > 120 then
    perform cabins_private.fail('INVALID_TITLE', 'Meeting purpose must be 3 to 120 characters.');
  end if;
  if p_attendees is null or p_attendees < 1 or p_attendees > 100 then
    perform cabins_private.fail('INVALID_ATTENDEES', 'Number of attendees must be between 1 and 100.');
  end if;
  if v_notes is not null and char_length(v_notes) > 1000 then
    perform cabins_private.fail('NOTES_TOO_LONG', 'Notes must be 1000 characters or fewer.');
  end if;
  if p_end_at <= p_start_at
     or date_trunc('minute', p_start_at) <> p_start_at
     or date_trunc('minute', p_end_at) <> p_end_at then
    perform cabins_private.fail('INVALID_TIME_RANGE', 'End time must be after start time, on whole minutes.');
  end if;

  v_minutes := extract(epoch from (p_end_at - p_start_at)) / 60;
  if v_minutes < s.min_booking_minutes then
    perform cabins_private.fail('DURATION_TOO_SHORT',
      format('Bookings must be at least %s minutes.', s.min_booking_minutes));
  end if;
  if v_minutes > least(s.max_booking_minutes, 120) then
    perform cabins_private.fail('DURATION_TOO_LONG',
      format('Bookings can be at most %s minutes.', least(s.max_booking_minutes, 120)));
  end if;

  if p_start_at < now() then
    perform cabins_private.fail('BOOKING_IN_PAST', 'The start time has already passed.');
  end if;
  if p_start_at > now() + make_interval(days => s.booking_horizon_days) then
    perform cabins_private.fail('BOOKING_TOO_FAR_AHEAD',
      format('Bookings can be made up to %s days ahead.', s.booking_horizon_days));
  end if;

  v_local_start := p_start_at at time zone s.timezone;
  v_local_end   := p_end_at   at time zone s.timezone;
  if not (extract(isodow from v_local_start)::smallint = any (s.working_days)) then
    perform cabins_private.fail('NON_WORKING_DAY', 'Cabins cannot be booked on this day.');
  end if;
  if v_local_end::date <> v_local_start::date
     or v_local_start::time < s.open_time
     or v_local_end::time > s.close_time then
    perform cabins_private.fail('OUTSIDE_OFFICE_HOURS',
      format('Bookings must be between %s and %s (%s).',
             to_char(s.open_time, 'HH24:MI'), to_char(s.close_time, 'HH24:MI'), s.timezone));
  end if;

  select * into v_cabin from cabins.cabins where id = p_cabin_id for share;
  if not found then
    perform cabins_private.fail('CABIN_NOT_FOUND', 'That cabin does not exist.');
  end if;
  if not v_cabin.is_active then
    perform cabins_private.fail('CABIN_INACTIVE', v_cabin.name || ' is currently not available for booking.');
  end if;
  if p_attendees > v_cabin.capacity then
    perform cabins_private.fail('CAPACITY_EXCEEDED',
      format('%s seats %s people.', v_cabin.name, v_cabin.capacity));
  end if;

  -- Serialise booking/maintenance changes per cabin for the rest of the transaction.
  perform cabins_private.lock_cabin(p_cabin_id);

  if exists (select 1 from cabins.maintenance_blocks m
              where m.cabin_id = p_cabin_id and m.status = 'active'
                and tstzrange(m.start_at, m.end_at, '[)') && tstzrange(p_start_at, p_end_at, '[)')) then
    perform cabins_private.fail('CABIN_UNDER_MAINTENANCE', v_cabin.name || ' is blocked for maintenance at that time.');
  end if;

  begin
    insert into cabins.bookings (cabin_id, user_id, start_at, end_at, title, attendees, notes)
    values (p_cabin_id, v_uid, p_start_at, p_end_at, v_title, p_attendees, v_notes)
    returning * into v_booking;
  exception when exclusion_violation then
    perform cabins_private.fail('CABIN_ALREADY_BOOKED',
      v_cabin.name || ' is already booked for part of that time. Please choose another slot or cabin.');
  end;

  perform cabins_private.queue_notification(v_booking.id, 'booking_confirmed');

  return jsonb_build_object(
    'id', v_booking.id, 'reference', v_booking.reference,
    'cabin_id', v_cabin.id, 'cabin_name', v_cabin.name,
    'start_at', v_booking.start_at, 'end_at', v_booking.end_at,
    'title', v_booking.title, 'attendees', v_booking.attendees, 'status', v_booking.status);
end;
$$;

-- -----------------------------------------------------------------------------
-- Cancellation (owner within the cut-off, or an administrator)
-- -----------------------------------------------------------------------------
create or replace function cabins.cancel_booking(p_booking_id uuid, p_reason text default null)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid      uuid := cabins_private.require_employee();
  v_admin    boolean := cabins_private.is_admin();
  s          cabins.office_settings := cabins_private.settings();
  v_booking  cabins.bookings;
  v_reason   text := nullif(left(btrim(coalesce(p_reason, '')), 300), '');
begin
  select * into v_booking from cabins.bookings where id = p_booking_id for update;
  -- Same error whether it does not exist or belongs to someone else: no probing.
  if not found or (v_booking.user_id <> v_uid and not v_admin) then
    perform cabins_private.fail('BOOKING_NOT_FOUND', 'Booking not found.');
  end if;
  if v_booking.status = 'cancelled' then
    perform cabins_private.fail('ALREADY_CANCELLED', 'This booking is already cancelled.');
  end if;
  if v_booking.end_at <= now() then
    perform cabins_private.fail('BOOKING_ALREADY_ENDED', 'Past bookings cannot be cancelled.');
  end if;
  if not v_admin
     and now() > v_booking.start_at - make_interval(mins => s.cancel_cutoff_minutes) then
    perform cabins_private.fail('CANCELLATION_WINDOW_CLOSED',
      case when s.cancel_cutoff_minutes = 0
           then 'Bookings can only be cancelled before they start.'
           else format('Bookings can only be cancelled up to %s minutes before they start.',
                       s.cancel_cutoff_minutes) end);
  end if;

  update cabins.bookings
     set status = 'cancelled', cancelled_at = now(), cancelled_by = v_uid, cancel_reason = v_reason
   where id = v_booking.id
   returning * into v_booking;

  perform cabins_private.queue_notification(v_booking.id,
    case when v_booking.user_id = v_uid then 'booking_cancelled' else 'booking_cancelled_by_admin' end);

  return jsonb_build_object('id', v_booking.id, 'reference', v_booking.reference,
                            'status', v_booking.status, 'cancelled_at', v_booking.cancelled_at);
end;
$$;

-- -----------------------------------------------------------------------------
-- Availability: every active employee sees busy intervals; meeting details only
-- for their own bookings (or for admins).
-- -----------------------------------------------------------------------------
create or replace function cabins.get_availability(p_from timestamptz, p_to timestamptz)
returns table (
  cabin_id     uuid,
  start_at     timestamptz,
  end_at       timestamptz,
  kind         text,
  is_mine      boolean,
  booking_id   uuid,
  reference    text,
  title        text,
  booker_name  text,
  reason       text)
language plpgsql
stable
security definer
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_uid   uuid := cabins_private.require_employee();
  v_admin boolean := cabins_private.is_admin();
  v_names boolean := (cabins_private.settings()).show_booker_names;
begin
  if p_from is null or p_to is null or p_to <= p_from or p_to - p_from > interval '32 days' then
    perform cabins_private.fail('INVALID_RANGE', 'Choose a range of up to 32 days.');
  end if;

  return query
    select b.cabin_id, b.start_at, b.end_at, 'booking'::text,
           b.user_id = v_uid,
           case when b.user_id = v_uid or v_admin then b.id end,
           case when b.user_id = v_uid or v_admin then b.reference end,
           case when b.user_id = v_uid or v_admin then b.title end,
           case when b.user_id = v_uid or v_admin or v_names
                then coalesce(nullif(p.full_name, ''), split_part(p.email, '@', 1)) end,
           null::text
      from cabins.bookings b
      join cabins.profiles p on p.id = b.user_id
     where b.status = 'confirmed' and b.start_at < p_to and b.end_at > p_from
    union all
    select m.cabin_id, m.start_at, m.end_at, 'maintenance'::text, false,
           null::uuid, null::text, null::text, null::text, m.reason
      from cabins.maintenance_blocks m
     where m.status = 'active' and m.start_at < p_to and m.end_at > p_from
    order by 2, 1;
end;
$$;

-- -----------------------------------------------------------------------------
-- Administration
-- -----------------------------------------------------------------------------
create or replace function cabins.admin_set_employee_status(
  p_user_id uuid, p_status text, p_cancel_future boolean default false)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid       uuid := cabins_private.require_admin();
  v_profile   cabins.profiles;
  v_cancelled integer := 0;
  r           record;
begin
  if p_user_id = v_uid then
    perform cabins_private.fail('CANNOT_CHANGE_OWN_ACCESS', 'You cannot change your own access.');
  end if;
  if p_status not in ('active', 'disabled') then
    perform cabins_private.fail('INVALID_STATUS', 'Status must be active or disabled.');
  end if;
  select * into v_profile from cabins.profiles where id = p_user_id for update;
  if not found then
    perform cabins_private.fail('EMPLOYEE_NOT_FOUND', 'Employee not found.');
  end if;

  if p_status = 'active' then
    if not cabins_private.is_allowed_email(v_profile.email) then
      perform cabins_private.fail('EMAIL_DOMAIN_NOT_ALLOWED', 'Only company e-mail addresses can be approved.');
    end if;
    update cabins.profiles
       set status = 'active', approved_at = now(), approved_by = v_uid,
           disabled_at = null, disabled_by = null
     where id = p_user_id;
  else
    update cabins.profiles
       set status = 'disabled', disabled_at = now(), disabled_by = v_uid
     where id = p_user_id;
    if p_cancel_future then
      for r in
        update cabins.bookings
           set status = 'cancelled', cancelled_at = now(), cancelled_by = v_uid,
               cancel_reason = 'Employee access disabled'
         where user_id = p_user_id and status = 'confirmed' and start_at > now()
        returning id
      loop
        v_cancelled := v_cancelled + 1;
        perform cabins_private.queue_notification(r.id, 'booking_cancelled_by_admin');
      end loop;
    end if;
  end if;

  return jsonb_build_object('user_id', p_user_id, 'status', p_status, 'cancelled_bookings', v_cancelled);
end;
$$;

create or replace function cabins.admin_set_role(p_user_id uuid, p_role text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid     uuid := cabins_private.require_admin();
  v_profile cabins.profiles;
begin
  if p_user_id = v_uid then
    perform cabins_private.fail('CANNOT_CHANGE_OWN_ROLE', 'You cannot change your own role.');
  end if;
  if p_role not in ('employee', 'admin') then
    perform cabins_private.fail('INVALID_ROLE', 'Role must be employee or admin.');
  end if;
  select * into v_profile from cabins.profiles where id = p_user_id for update;
  if not found then
    perform cabins_private.fail('EMPLOYEE_NOT_FOUND', 'Employee not found.');
  end if;
  if p_role = 'admin' and v_profile.status <> 'active' then
    perform cabins_private.fail('EMPLOYEE_NOT_ACTIVE', 'Only active employees can be made administrators.');
  end if;
  update cabins.profiles set role = p_role where id = p_user_id;
  return jsonb_build_object('user_id', p_user_id, 'role', p_role);
end;
$$;

create or replace function cabins.admin_create_maintenance(
  p_cabin_id uuid, p_start_at timestamptz, p_end_at timestamptz, p_reason text,
  p_cancel_conflicts boolean default false)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid       uuid := cabins_private.require_admin();
  v_reason    text := btrim(coalesce(p_reason, ''));
  v_conflicts integer;
  v_id        uuid;
  r           record;
begin
  if p_cabin_id is null or p_start_at is null or p_end_at is null or p_end_at <= p_start_at then
    perform cabins_private.fail('INVALID_TIME_RANGE', 'Choose a cabin and a valid time range.');
  end if;
  if p_end_at <= now() then
    perform cabins_private.fail('MAINTENANCE_IN_PAST', 'Maintenance must end in the future.');
  end if;
  if char_length(v_reason) < 3 or char_length(v_reason) > 200 then
    perform cabins_private.fail('INVALID_REASON', 'Reason must be 3 to 200 characters.');
  end if;
  if not exists (select 1 from cabins.cabins where id = p_cabin_id) then
    perform cabins_private.fail('CABIN_NOT_FOUND', 'That cabin does not exist.');
  end if;

  perform cabins_private.lock_cabin(p_cabin_id);

  select count(*) into v_conflicts
    from cabins.bookings b
   where b.cabin_id = p_cabin_id and b.status = 'confirmed'
     and tstzrange(b.start_at, b.end_at, '[)') && tstzrange(p_start_at, p_end_at, '[)');

  if v_conflicts > 0 and not p_cancel_conflicts then
    perform cabins_private.fail('MAINTENANCE_CONFLICTS_BOOKINGS',
      format('%s confirmed booking(s) overlap this period. Confirm to cancel them and notify the owners.', v_conflicts));
  end if;

  insert into cabins.maintenance_blocks (cabin_id, start_at, end_at, reason, created_by)
  values (p_cabin_id, p_start_at, p_end_at, v_reason, v_uid)
  returning id into v_id;

  if v_conflicts > 0 then
    for r in
      update cabins.bookings b
         set status = 'cancelled', cancelled_at = now(), cancelled_by = v_uid,
             cancel_reason = left('Maintenance: ' || v_reason, 300)
       where b.cabin_id = p_cabin_id and b.status = 'confirmed'
         and tstzrange(b.start_at, b.end_at, '[)') && tstzrange(p_start_at, p_end_at, '[)')
      returning b.id
    loop
      perform cabins_private.queue_notification(r.id, 'maintenance_cancellation');
    end loop;
  end if;

  return jsonb_build_object('id', v_id, 'cancelled_bookings', v_conflicts);
end;
$$;

create or replace function cabins.admin_remove_maintenance(p_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid uuid := cabins_private.require_admin();
begin
  update cabins.maintenance_blocks
     set status = 'removed', removed_at = now(), removed_by = v_uid
   where id = p_id and status = 'active';
  if not found then
    perform cabins_private.fail('MAINTENANCE_NOT_FOUND', 'Active maintenance block not found.');
  end if;
  return jsonb_build_object('id', p_id, 'status', 'removed');
end;
$$;

create or replace function cabins.admin_retry_failed_notifications()
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid   uuid := cabins_private.require_admin();
  v_count integer;
begin
  update cabins.notification_logs
     set status = 'pending', attempts = 0, next_attempt_at = now(), last_error = null
   where status = 'failed';
  get diagnostics v_count = row_count;
  perform cabins_private.write_audit('notifications.retry_requested', 'notification_logs', null,
                              jsonb_build_object('count', v_count));
  return v_count;
end;
$$;

-- -----------------------------------------------------------------------------
-- Notification worker API (service_role only — called by the server, never a browser)
-- -----------------------------------------------------------------------------
-- Returns a JSON array (not SETOF) so PostgREST hands back a plain list.
create or replace function cabins.claim_notifications(p_limit integer default 20)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_ids uuid[];
  s     cabins.office_settings := cabins_private.settings();
begin
  with due as (
    select n.id
      from cabins.notification_logs n
     where n.attempts < 5
       and ((n.status in ('pending', 'failed') and n.next_attempt_at <= now())
            or (n.status = 'sending' and n.locked_at < now() - interval '10 minutes'))
     order by n.next_attempt_at
     limit greatest(1, least(coalesce(p_limit, 20), 100))
     for update skip locked
  ), upd as (
    update cabins.notification_logs n
       set status = 'sending', attempts = n.attempts + 1, locked_at = now()
      from due where n.id = due.id
    returning n.id
  )
  select coalesce(array_agg(id), '{}') into v_ids from upd;

  return coalesce((
    select jsonb_agg(jsonb_build_object(
      'id', n.id, 'kind', n.kind, 'recipient_email', n.recipient_email, 'attempts', n.attempts,
      'booking_id', b.id, 'reference', b.reference, 'booking_status', b.status,
      'title', b.title, 'start_at', b.start_at, 'end_at', b.end_at,
      'cancel_reason', b.cancel_reason,
      'cabin_name', c.name, 'cabin_location', c.location,
      'employee_name', p.full_name,
      'timezone', s.timezone, 'app_name', s.app_name, 'company_name', s.company_name)
      order by n.created_at)
      from cabins.notification_logs n
      join cabins.bookings b on b.id = n.booking_id
      join cabins.cabins c   on c.id = b.cabin_id
      join cabins.profiles p on p.id = b.user_id
     where n.id = any (v_ids)), '[]'::jsonb);
end;
$$;

create or replace function cabins.complete_notification(p_id uuid, p_outcome text, p_error text default null)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  if p_outcome = 'sent' then
    update cabins.notification_logs
       set status = 'sent', sent_at = now(), last_error = null, locked_at = null
     where id = p_id;
  elsif p_outcome = 'skipped' then
    update cabins.notification_logs
       set status = 'skipped', last_error = left(p_error, 500), locked_at = null
     where id = p_id;
  elsif p_outcome = 'failed' then
    update cabins.notification_logs
       set status = 'failed', last_error = left(coalesce(p_error, 'unknown error'), 500), locked_at = null,
           next_attempt_at = now() + make_interval(mins => least(60, power(2, attempts)::integer))
     where id = p_id;
  else
    raise exception 'INVALID_OUTCOME';
  end if;
end;
$$;

create or replace function cabins.queue_due_reminders()
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  s       cabins.office_settings := cabins_private.settings();
  v_count integer;
begin
  if s.reminder_minutes_before = 0 then
    return 0;
  end if;
  insert into cabins.notification_logs (booking_id, kind, recipient_email)
  select b.id, 'reminder', p.email
    from cabins.bookings b join cabins.profiles p on p.id = b.user_id
   where b.status = 'confirmed'
     and b.start_at > now()
     and b.start_at <= now() + make_interval(mins => s.reminder_minutes_before)
  on conflict (booking_id, kind) do nothing;
  get diagnostics v_count = row_count;
  return v_count;
end;
$$;

-- -----------------------------------------------------------------------------
-- First administrator. NOT callable through the API: run it once from the
-- Supabase SQL editor (or psql as the database owner) after that person has
-- signed up and verified their e-mail. Refuses if an admin already exists.
-- -----------------------------------------------------------------------------
create or replace function cabins_private.bootstrap_first_admin(p_email text)
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_email text := lower(btrim(p_email));
  v_user  auth.users%rowtype;
begin
  if exists (select 1 from cabins.profiles where role = 'admin' and status = 'active') then
    raise exception 'ADMIN_ALREADY_EXISTS'
      using detail = 'An active administrator exists; promote further admins from the admin panel.';
  end if;
  select * into v_user from auth.users where lower(email) = v_email;
  if not found then
    raise exception 'USER_NOT_FOUND' using detail = 'Sign up with this address in the app first.';
  end if;
  if v_user.email_confirmed_at is null then
    raise exception 'EMAIL_NOT_VERIFIED' using detail = 'Verify the e-mail address before promoting it.';
  end if;
  if not cabins_private.is_allowed_email(v_email) then
    raise exception 'EMAIL_DOMAIN_NOT_ALLOWED';
  end if;
  perform cabins_private.ensure_profile(v_user.id);
  update cabins.profiles
     set role = 'admin', status = 'active', approved_at = now(), approved_by = null,
         disabled_at = null, disabled_by = null
   where id = v_user.id;
  perform cabins_private.write_audit('admin.bootstrapped', 'profiles', v_user.id::text,
                              jsonb_build_object('email', v_email));
  return 'Administrator created: ' || v_email;
end;
$$;

-- ===================== migrations/0005_rls_and_grants.sql =====================
-- =============================================================================
-- 0005_rls_and_grants.sql
-- Row Level Security and least-privilege grants.
--
-- How this interacts with Supabase Auth: the app calls PostgREST with the
-- signed-in user's JWT. PostgREST verifies the signature, switches to the
-- "authenticated" role and exposes the claims to auth.uid(). Every policy
-- below keys off auth.uid() plus cabins_private.is_active_employee()/is_admin(),
-- which re-read the profile and auth.users on every statement — so disabling
-- an employee or un-verifying an address takes effect immediately.
-- The "anon" role (no JWT) gets no table access at all.
-- =============================================================================

-- Start from zero: Supabase grants broad default privileges on public tables.
revoke all on cabins.office_settings, cabins.audit_logs, cabins.profiles, cabins.employee_allowlist,
              cabins.cabins, cabins.maintenance_blocks, cabins.bookings, cabins.notification_logs
  from anon, authenticated;
revoke all on sequence cabins.booking_ref_seq from anon, authenticated;

alter table cabins.office_settings     enable row level security;
alter table cabins.audit_logs          enable row level security;
alter table cabins.profiles            enable row level security;
alter table cabins.employee_allowlist  enable row level security;
alter table cabins.cabins              enable row level security;
alter table cabins.maintenance_blocks  enable row level security;
alter table cabins.bookings            enable row level security;
alter table cabins.notification_logs   enable row level security;

-- ---------------------------------------------------------------- office_settings
grant select on cabins.office_settings to authenticated;
grant update (app_name, company_name, logo_url, timezone, open_time, close_time, working_days,
              min_booking_minutes, max_booking_minutes, slot_minutes, booking_horizon_days,
              cancel_cutoff_minutes, reminder_minutes_before, show_booker_names, access_policy)
  on cabins.office_settings to authenticated;
-- allowed_email_domains is deliberately NOT updatable through the API.

drop policy if exists settings_read on cabins.office_settings;
create policy settings_read on cabins.office_settings
  for select to authenticated using (cabins_private.is_active_employee());

drop policy if exists settings_admin_update on cabins.office_settings;
create policy settings_admin_update on cabins.office_settings
  for update to authenticated using (cabins_private.is_admin()) with check (cabins_private.is_admin());

drop trigger if exists office_settings_audit on cabins.office_settings;
create trigger office_settings_audit after update on cabins.office_settings
  for each row execute function cabins_private.audit_row_change();

-- ---------------------------------------------------------------- audit_logs
grant select on cabins.audit_logs to authenticated;
drop policy if exists audit_admin_read on cabins.audit_logs;
create policy audit_admin_read on cabins.audit_logs
  for select to authenticated using (cabins_private.is_admin());

-- ---------------------------------------------------------------- profiles
grant select on cabins.profiles to authenticated;
grant update (full_name, department) on cabins.profiles to authenticated;

drop policy if exists profiles_read_own on cabins.profiles;
create policy profiles_read_own on cabins.profiles
  for select to authenticated using (id = auth.uid());

drop policy if exists profiles_admin_read on cabins.profiles;
create policy profiles_admin_read on cabins.profiles
  for select to authenticated using (cabins_private.is_admin());

drop policy if exists profiles_update_own on cabins.profiles;
create policy profiles_update_own on cabins.profiles
  for update to authenticated
  using (id = auth.uid() and cabins_private.is_active_employee())
  with check (id = auth.uid());

-- ---------------------------------------------------------------- employee_allowlist
grant select, insert, delete on cabins.employee_allowlist to authenticated;
grant update (note) on cabins.employee_allowlist to authenticated;

drop policy if exists allowlist_admin_all on cabins.employee_allowlist;
create policy allowlist_admin_all on cabins.employee_allowlist
  for all to authenticated using (cabins_private.is_admin()) with check (cabins_private.is_admin());

-- ---------------------------------------------------------------- cabins
grant select on cabins.cabins to authenticated;
grant update (name, location, capacity, equipment, is_active, sort_order) on cabins.cabins to authenticated;

drop policy if exists cabins_read on cabins.cabins;
create policy cabins_read on cabins.cabins
  for select to authenticated using (cabins_private.is_active_employee());

drop policy if exists cabins_admin_update on cabins.cabins;
create policy cabins_admin_update on cabins.cabins
  for update to authenticated using (cabins_private.is_admin()) with check (cabins_private.is_admin());

-- ---------------------------------------------------------------- maintenance_blocks
-- Writes only via admin_create_maintenance / admin_remove_maintenance.
grant select on cabins.maintenance_blocks to authenticated;
drop policy if exists maintenance_read on cabins.maintenance_blocks;
create policy maintenance_read on cabins.maintenance_blocks
  for select to authenticated using (cabins_private.is_active_employee());

-- ---------------------------------------------------------------- bookings
-- Read: own rows, or everything for admins. Other people's bookings are only
-- visible as anonymous busy intervals through get_availability().
-- Writes: only via book_cabin / cancel_booking / admin functions.
grant select on cabins.bookings to authenticated;

drop policy if exists bookings_read_own on cabins.bookings;
create policy bookings_read_own on cabins.bookings
  for select to authenticated using (user_id = auth.uid() and cabins_private.is_active_employee());

drop policy if exists bookings_admin_read on cabins.bookings;
create policy bookings_admin_read on cabins.bookings
  for select to authenticated using (cabins_private.is_admin());

-- ---------------------------------------------------------------- notification_logs
grant select on cabins.notification_logs to authenticated;
drop policy if exists notifications_admin_read on cabins.notification_logs;
create policy notifications_admin_read on cabins.notification_logs
  for select to authenticated using (cabins_private.is_admin());

-- ---------------------------------------------------------------- service_role
-- Used only by the server-side notification worker. It bypasses RLS by design.
grant select, insert, update on cabins.notification_logs to service_role;
grant select on cabins.bookings, cabins.cabins, cabins.profiles, cabins.office_settings to service_role;

-- ---------------------------------------------------------------- functions
revoke execute on all functions in schema cabins         from public, anon;
revoke execute on all functions in schema cabins_private from public, anon;

grant execute on function cabins.get_public_branding()                         to anon, authenticated;
grant execute on function cabins.get_my_access()                               to authenticated;
grant execute on function cabins.book_cabin(uuid, timestamptz, timestamptz, text, integer, text) to authenticated;
grant execute on function cabins.cancel_booking(uuid, text)                    to authenticated;
grant execute on function cabins.get_availability(timestamptz, timestamptz)    to authenticated;
grant execute on function cabins.admin_set_employee_status(uuid, text, boolean) to authenticated;
grant execute on function cabins.admin_set_role(uuid, text)                    to authenticated;
grant execute on function cabins.admin_create_maintenance(uuid, timestamptz, timestamptz, text, boolean) to authenticated;
grant execute on function cabins.admin_remove_maintenance(uuid)                to authenticated;
grant execute on function cabins.admin_retry_failed_notifications()            to authenticated;

grant execute on function cabins.claim_notifications(integer)                  to service_role;
grant execute on function cabins.complete_notification(uuid, text, text)       to service_role;
grant execute on function cabins.queue_due_reminders()                         to service_role;

-- Helpers referenced from policies must be executable by the caller's role.
grant execute on function cabins_private.is_active_employee(), cabins_private.is_admin(),
                         cabins_private.is_allowed_email(text), cabins_private.email_domain(text),
                         cabins_private.settings()
  to authenticated, service_role;
-- cabins_private.bootstrap_first_admin(text) is intentionally granted to nobody.

-- ===================== migrations/0006_seed_cabins.sql =====================
-- =============================================================================
-- 0006_seed_cabins.sql
-- The ten meeting cabins. Idempotent: re-running never overwrites details an
-- administrator has edited. Names, locations, capacity and equipment are
-- placeholders — edit them from Admin › Cabins.
-- =============================================================================

insert into cabins.cabins (code, name, location, capacity, equipment, sort_order) values
  ('C01', 'Cabin 01', 'Floor 1 · East wing',  4, '{"TV display"}',                                   1),
  ('C02', 'Cabin 02', 'Floor 1 · East wing',  4, '{"TV display"}',                                   2),
  ('C03', 'Cabin 03', 'Floor 1 · West wing',  6, '{"TV display","Whiteboard"}',                      3),
  ('C04', 'Cabin 04', 'Floor 1 · West wing',  6, '{"Video conferencing","Whiteboard"}',              4),
  ('C05', 'Cabin 05', 'Floor 2 · East wing',  8, '{"Projector","Whiteboard"}',                       5),
  ('C06', 'Cabin 06', 'Floor 2 · East wing',  8, '{"Video conferencing","TV display"}',              6),
  ('C07', 'Cabin 07', 'Floor 2 · West wing',  4, '{"TV display"}',                                   7),
  ('C08', 'Cabin 08', 'Floor 2 · West wing',  2, '{}',                                               8),
  ('C09', 'Cabin 09', 'Floor 3 · Board room', 12, '{"Video conferencing","Projector","Whiteboard"}', 9),
  ('C10', 'Cabin 10', 'Floor 3 · Focus room', 2, '{}',                                               10)
on conflict (code) do nothing;

select code, name from cabins.cabins order by code;
