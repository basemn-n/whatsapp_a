-- Privacy-preserving AI usage metering. Stores token counts only; never stores prompts,
-- completions, phone numbers, Telegram chat IDs, or media.
create table if not exists public.ai_usage_logs (
    id bigint generated always as identity primary key,
    created_at timestamptz not null default now(),
    business_id uuid null,
    provider text not null check (provider in ('groq')),
    model text not null,
    operation text not null check (operation in ('text', 'image')),
    input_tokens integer not null default 0 check (input_tokens >= 0),
    output_tokens integer not null default 0 check (output_tokens >= 0),
    total_tokens integer not null default 0 check (total_tokens >= 0),
    estimated_cost_usd numeric(14, 8) null check (estimated_cost_usd >= 0)
);

create index if not exists ai_usage_logs_created_at_idx
    on public.ai_usage_logs (created_at desc);
create index if not exists ai_usage_logs_business_created_at_idx
    on public.ai_usage_logs (business_id, created_at desc);

alter table public.ai_usage_logs enable row level security;

revoke all on table public.ai_usage_logs from anon, authenticated;
grant select, insert on table public.ai_usage_logs to service_role;
grant usage, select on sequence public.ai_usage_logs_id_seq to service_role;

drop policy if exists "service role manages AI usage logs" on public.ai_usage_logs;
create policy "service role manages AI usage logs"
    on public.ai_usage_logs
    for all
    to service_role
    using (true)
    with check (true);
