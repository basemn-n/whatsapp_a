-- Map each external messaging account to exactly one merchant store.
-- Run once in Supabase SQL Editor. Existing product/order data is untouched.
create table if not exists public.channel_connections (
    id uuid primary key default gen_random_uuid(),
    channel text not null,
    external_account_id text not null,
    business_id uuid not null references public.businesses(id) on delete cascade,
    created_at timestamptz not null default now(),
    constraint channel_connections_channel_check
        check (channel in ('telegram_products', 'whatsapp', 'telegram')),
    constraint channel_connections_external_account_unique
        unique (channel, external_account_id)
);

create index if not exists channel_connections_business_id_idx
    on public.channel_connections (business_id);

alter table public.channel_connections enable row level security;

-- The application accesses this mapping through the Supabase service role.
-- Do not expose this table to anon/authenticated clients.
revoke all on table public.channel_connections from anon, authenticated;
grant select, insert, update, delete on table public.channel_connections to service_role;
