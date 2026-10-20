-- Merchant self-registration with administrator approval.
-- Run this migration in Supabase SQL Editor before deploying the matching app changes.
create table if not exists public.merchant_signup_requests (
    id uuid primary key default gen_random_uuid(),
    store_name text not null check (char_length(trim(store_name)) between 2 and 160),
    industry text not null default '',
    username text not null,
    password_hash text not null,
    status text not null default 'pending'
        check (status in ('pending', 'approved', 'rejected')),
    rejection_reason text not null default '',
    business_id uuid references public.businesses(id) on delete set null,
    created_at timestamptz not null default now(),
    reviewed_at timestamptz
);

create unique index if not exists merchant_signup_pending_username_unique
    on public.merchant_signup_requests (lower(username))
    where status = 'pending';

create index if not exists merchant_signup_requests_status_created_idx
    on public.merchant_signup_requests (status, created_at desc);

alter table public.merchant_signup_requests enable row level security;
revoke all on public.merchant_signup_requests from anon, authenticated;
grant all on public.merchant_signup_requests to service_role;
