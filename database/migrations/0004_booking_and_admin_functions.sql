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
