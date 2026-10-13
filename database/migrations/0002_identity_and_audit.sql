-- =============================================================================
-- 0002_identity_and_audit.sql
-- Audit log, employee profiles, allowlist, auth.users triggers and the
-- authorization helpers used by every RLS policy.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Audit log (append-only)
-- -----------------------------------------------------------------------------
create table if not exists public.audit_logs (
  id           bigint generated always as identity primary key,
  occurred_at  timestamptz not null default now(),
  actor_id     uuid,                       -- null = system / database owner
  actor_email  text,
  action       text not null,
  entity_type  text not null,
  entity_id    text,
  details      jsonb not null default '{}'::jsonb
);
create index if not exists audit_logs_occurred_at_idx on public.audit_logs (occurred_at desc);
create index if not exists audit_logs_entity_idx on public.audit_logs (entity_type, entity_id);

create or replace function private.block_audit_mutation()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception 'AUDIT_LOG_IMMUTABLE' using detail = 'Audit log entries cannot be changed or deleted.';
end;
$$;

drop trigger if exists audit_logs_immutable on public.audit_logs;
create trigger audit_logs_immutable
  before update or delete on public.audit_logs
  for each row execute function private.block_audit_mutation();

create or replace function private.write_audit(
  p_action text, p_entity_type text, p_entity_id text, p_details jsonb default '{}'::jsonb)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid uuid := auth.uid();
begin
  insert into public.audit_logs (actor_id, actor_email, action, entity_type, entity_id, details)
  values (v_uid,
          (select u.email from auth.users u where u.id = v_uid),
          p_action, p_entity_type, p_entity_id, coalesce(p_details, '{}'::jsonb));
end;
$$;

-- Generic row-change audit trigger. Column names in TG_ARGV are redacted
-- (their values are never copied into the log; only the fact they changed).
create or replace function private.audit_row_change()
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

  perform private.write_audit(v_action, tg_table_name, v_entity_id, v_changes);
  return coalesce(new, old);
end;
$$;

-- -----------------------------------------------------------------------------
-- Profiles
-- -----------------------------------------------------------------------------
create table if not exists public.profiles (
  id           uuid primary key references auth.users (id) on delete restrict,
  email        text not null unique check (email = lower(email)),
  full_name    text not null default '' check (char_length(full_name) <= 120),
  department   text check (department is null or char_length(department) <= 80),
  role         text not null default 'employee' check (role in ('employee', 'admin')),
  status       text not null default 'pending' check (status in ('pending', 'active', 'disabled')),
  approved_at  timestamptz,
  approved_by  uuid references public.profiles (id),
  disabled_at  timestamptz,
  disabled_by  uuid references public.profiles (id),
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create index if not exists profiles_status_idx on public.profiles (status);

drop trigger if exists profiles_touch on public.profiles;
create trigger profiles_touch before update on public.profiles
  for each row execute function private.touch_updated_at();

-- Defence in depth: role/status can never be changed by an API caller directly,
-- only by SECURITY DEFINER functions (which run as the table owner).
create or replace function private.guard_profile_privileged_columns()
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

drop trigger if exists profiles_guard on public.profiles;
create trigger profiles_guard before update on public.profiles
  for each row execute function private.guard_profile_privileged_columns();

drop trigger if exists profiles_audit on public.profiles;
create trigger profiles_audit after insert or update on public.profiles
  for each row execute function private.audit_row_change();

-- -----------------------------------------------------------------------------
-- Allowlist of pre-approved employee addresses (used by access_policy='approval')
-- -----------------------------------------------------------------------------
create table if not exists public.employee_allowlist (
  email       text primary key check (email = lower(btrim(email))),
  note        text check (note is null or char_length(note) <= 200),
  added_by    uuid references public.profiles (id),
  created_at  timestamptz not null default now()
);

create or replace function private.validate_allowlist_entry()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  new.email := lower(btrim(new.email));
  if not private.is_allowed_email(new.email) then
    raise exception 'EMAIL_DOMAIN_NOT_ALLOWED'
      using detail = 'Only addresses on the company domain can be allowlisted.';
  end if;
  new.added_by := coalesce(new.added_by, auth.uid());
  return new;
end;
$$;

drop trigger if exists employee_allowlist_validate on public.employee_allowlist;
create trigger employee_allowlist_validate before insert or update on public.employee_allowlist
  for each row execute function private.validate_allowlist_entry();

drop trigger if exists employee_allowlist_audit on public.employee_allowlist;
create trigger employee_allowlist_audit after insert or update or delete on public.employee_allowlist
  for each row execute function private.audit_row_change();

-- -----------------------------------------------------------------------------
-- Activation logic shared by the auth.users triggers and the allowlist.
-- A profile becomes 'active' only when ALL of these hold:
--   * the auth user has a confirmed e-mail (ownership verified by Supabase Auth)
--   * the address is on an allowed company domain
--   * policy = 'domain', or the address is on employee_allowlist
-- Disabled profiles are never re-activated automatically.
-- -----------------------------------------------------------------------------
create or replace function private.sync_profile_activation(p_user_id uuid)
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

  update public.profiles set email = v_email
   where id = p_user_id and email is distinct from v_email;

  if v_user.email_confirmed_at is not null
     and private.is_allowed_email(v_email)
     and ((private.settings()).access_policy = 'domain'
          or exists (select 1 from public.employee_allowlist a where a.email = v_email)) then
    update public.profiles
       set status = 'active', approved_at = now(), approved_by = null
     where id = p_user_id and status = 'pending';
  end if;
end;
$$;

-- BEFORE INSERT/UPDATE OF email on auth.users: refuse non-company addresses even
-- if someone calls the Supabase Auth API directly (bypassing this app).
create or replace function private.enforce_auth_email_domain()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if not private.is_allowed_email(new.email) then
    raise exception 'EMAIL_DOMAIN_NOT_ALLOWED'
      using errcode = '42501',
            detail = 'Only company e-mail addresses may register.';
  end if;
  return new;
end;
$$;

create or replace function private.handle_auth_user_insert()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_name text := left(btrim(coalesce(new.raw_user_meta_data ->> 'full_name', '')), 120);
begin
  insert into public.profiles (id, email, full_name)
  values (new.id, lower(btrim(new.email)), v_name)
  on conflict (id) do nothing;
  perform private.sync_profile_activation(new.id);
  return new;
end;
$$;

create or replace function private.handle_auth_user_update()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  perform private.sync_profile_activation(new.id);
  return new;
end;
$$;

drop trigger if exists wealthy_enforce_email_domain on auth.users;
create trigger wealthy_enforce_email_domain
  before insert or update of email on auth.users
  for each row execute function private.enforce_auth_email_domain();

drop trigger if exists wealthy_on_auth_user_created on auth.users;
create trigger wealthy_on_auth_user_created
  after insert on auth.users
  for each row execute function private.handle_auth_user_insert();

drop trigger if exists wealthy_on_auth_user_updated on auth.users;
create trigger wealthy_on_auth_user_updated
  after update of email, email_confirmed_at on auth.users
  for each row execute function private.handle_auth_user_update();

-- When an admin allowlists an address whose owner already verified it, activate them.
create or replace function private.handle_allowlist_insert()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  perform private.sync_profile_activation(p.id)
     from public.profiles p where p.email = new.email;
  return new;
end;
$$;

drop trigger if exists employee_allowlist_activate on public.employee_allowlist;
create trigger employee_allowlist_activate after insert on public.employee_allowlist
  for each row execute function private.handle_allowlist_insert();

-- -----------------------------------------------------------------------------
-- Authorization helpers used by RLS policies and RPCs
-- -----------------------------------------------------------------------------
create or replace function private.is_active_employee()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
      from public.profiles p
      join auth.users u on u.id = p.id
     where p.id = auth.uid()
       and p.status = 'active'
       and u.email_confirmed_at is not null
       and u.deleted_at is null
       and (u.banned_until is null or u.banned_until < now())
       and lower(u.email) = p.email
       and private.is_allowed_email(u.email)
  )
$$;

create or replace function private.is_admin()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select private.is_active_employee()
     and exists (select 1 from public.profiles p where p.id = auth.uid() and p.role = 'admin')
$$;

grant execute on function private.is_active_employee() to authenticated, service_role;
grant execute on function private.is_admin()           to authenticated, service_role;
