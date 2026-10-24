-- Run once in the Supabase SQL editor after the API has started and created its tables.
-- Turns on row level security so the public anon key can never read these tables.
-- The API connects with the database password, which bypasses RLS, so it keeps working.
alter table public."orgs" enable row level security;
alter table public."agents" enable row level security;
alter table public."consents" enable row level security;
alter table public."idempotency_keys" enable row level security;
alter table public."members" enable row level security;
alter table public."webhook_deliveries" enable row level security;
alter table public."calls" enable row level security;
alter table public."bookings" enable row level security;
alter table public."leads" enable row level security;
