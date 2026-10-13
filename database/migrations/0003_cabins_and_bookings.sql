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
