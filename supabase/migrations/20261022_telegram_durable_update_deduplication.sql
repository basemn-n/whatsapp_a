-- Durable Telegram webhook update deduplication across Render restarts.
-- Existing products and orders are not modified.
create table if not exists public.telegram_processed_updates (
    update_id bigint primary key,
    processed_at timestamptz not null default now()
);

alter table public.telegram_processed_updates enable row level security;
revoke all on table public.telegram_processed_updates from anon, authenticated;
grant select, insert, delete on table public.telegram_processed_updates to service_role;

create or replace function public.claim_telegram_update(p_update_id bigint)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
    insert into public.telegram_processed_updates (update_id)
    values (p_update_id)
    on conflict (update_id) do nothing;

    return found;
end;
$$;

create or replace function public.release_telegram_update(p_update_id bigint)
returns void
language sql
security definer
set search_path = public
as $$
    delete from public.telegram_processed_updates
    where update_id = p_update_id;
$$;

revoke all on function public.claim_telegram_update(bigint) from public, anon, authenticated;
revoke all on function public.release_telegram_update(bigint) from public, anon, authenticated;
grant execute on function public.claim_telegram_update(bigint) to service_role;
grant execute on function public.release_telegram_update(bigint) to service_role;
