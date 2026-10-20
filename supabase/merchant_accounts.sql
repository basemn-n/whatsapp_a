-- Merchant accounts for tenant-scoped dashboard access.
-- Run this migration once in Supabase SQL Editor before enabling merchant login.
create table if not exists public.merchant_accounts (
    id uuid primary key default gen_random_uuid(),
    business_id uuid not null references public.businesses(id) on delete cascade,
    email text not null unique,
    password_hash text not null,
    is_active boolean not null default true,
    created_at timestamptz not null default now()
);

create index if not exists merchant_accounts_business_id_idx
    on public.merchant_accounts (business_id);

alter table public.merchant_accounts enable row level security;

-- The application accesses this table with the server-side service-role key.
-- Never expose that key in the browser or send it to a merchant.
revoke all on table public.merchant_accounts from anon, authenticated;
grant all on table public.merchant_accounts to service_role;
