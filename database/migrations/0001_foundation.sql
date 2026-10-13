-- =============================================================================
-- 0001_foundation.sql
-- Extensions, the private helper schema, office settings and generic triggers.
-- Run as the database owner (the "postgres" role in Supabase).
-- =============================================================================

create schema if not exists extensions;
create extension if not exists btree_gist with schema extensions;

-- Internal helpers live in "private", which PostgREST does not expose.
create schema if not exists private;
revoke all on schema private from public;
grant usage on schema private to authenticated, service_role;

-- Functions are not executable by PUBLIC unless granted explicitly.
alter default privileges in schema private revoke execute on functions from public;
alter default privileges in schema public  revoke execute on functions from public;

-- -----------------------------------------------------------------------------
-- updated_at trigger
-- -----------------------------------------------------------------------------
create or replace function private.touch_updated_at()
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
create table if not exists public.office_settings (
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

create or replace function private.validate_office_settings()
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

drop trigger if exists office_settings_validate on public.office_settings;
create trigger office_settings_validate
  before insert or update on public.office_settings
  for each row execute function private.validate_office_settings();

insert into public.office_settings (id) values (true) on conflict (id) do nothing;

create or replace function private.settings()
returns public.office_settings
language sql
stable
security definer
set search_path = ''
as $$
  select s from public.office_settings s where s.id
$$;

-- -----------------------------------------------------------------------------
-- E-mail helpers
-- -----------------------------------------------------------------------------
create or replace function private.email_domain(p_email text)
returns text
language sql
immutable
set search_path = ''
as $$
  select lower(substring(btrim(p_email) from '@([^@]+)$'))
$$;

-- Exact (not suffix) match against the configured domains, so
-- "x@evilwealthy.in" and "x@wealthy.in.example.com" are rejected.
create or replace function private.is_allowed_email(p_email text)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select coalesce(
    p_email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'
    and private.email_domain(p_email) = any ((private.settings()).allowed_email_domains),
    false)
$$;

grant execute on function private.email_domain(text)      to authenticated, service_role;
grant execute on function private.is_allowed_email(text)  to authenticated, service_role;
grant execute on function private.settings()              to authenticated, service_role;
