-- Run this script once in Supabase SQL Editor.
-- Stores unfinished Telegram orders across Render restarts.
create table if not exists public.telegram_pending_orders (
    chat_id bigint primary key,
    business_id uuid not null references public.businesses(id) on delete cascade,
    state jsonb not null,
    updated_at timestamptz not null default now()
);

create index if not exists telegram_pending_orders_updated_at_idx
    on public.telegram_pending_orders (updated_at);

create or replace function public.set_telegram_pending_orders_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists telegram_pending_orders_updated_at
    on public.telegram_pending_orders;

create trigger telegram_pending_orders_updated_at
before update on public.telegram_pending_orders
for each row
execute function public.set_telegram_pending_orders_updated_at();

-- The app uses the Supabase service-role key server-side.
-- RLS is enabled with no client-facing policies, so browser clients cannot read these states.
alter table public.telegram_pending_orders enable row level security;
