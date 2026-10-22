-- Per-store assistant instructions, isolated by business_id.
create table if not exists public.business_settings (
    business_id uuid primary key references public.businesses(id) on delete cascade,
    business_description text not null default '',
    business_policies text not null default '',
    business_hours text not null default '',
    currency text not null default 'YER'
        check (currency in ('YER', 'SAR', 'USD', 'AED', 'EGP')),
    updated_at timestamptz not null default now()
);

alter table public.business_settings enable row level security;

revoke all privileges on table public.business_settings from anon, authenticated;
grant all privileges on table public.business_settings to service_role;

create or replace function public.set_business_settings_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists business_settings_updated_at on public.business_settings;
create trigger business_settings_updated_at
before update on public.business_settings
for each row execute function public.set_business_settings_updated_at();
