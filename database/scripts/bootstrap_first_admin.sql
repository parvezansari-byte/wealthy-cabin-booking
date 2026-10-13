-- =============================================================================
-- Create the FIRST administrator (run once).
--
-- 1. The person signs up in the app with their @wealthy.in address and enters
--    the 6-digit verification code (so ownership of the mailbox is proven).
-- 2. A Supabase project owner opens Supabase Dashboard › SQL Editor (which runs
--    as the database owner) and runs the statement below with that address.
--
-- The function refuses if: an active admin already exists, the user has not
-- signed up, the e-mail is not verified, or the domain is not allowed.
-- It is not callable through the API by anyone. Further admins are promoted
-- by an existing admin in Administration › Employees (audited).
-- =============================================================================

select cabins_private.bootstrap_first_admin('first.admin@wealthy.in');   -- ← replace the address

-- Check:
select email, role, status from cabins.profiles where role = 'admin';
