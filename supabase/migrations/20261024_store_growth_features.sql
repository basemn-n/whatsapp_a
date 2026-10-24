-- Store-specific shopping advisor, consent-based Telegram follow-ups, loyalty and coupons.
-- Run once in Supabase SQL Editor before using these features.

create table if not exists public.store_growth_settings (
    business_id uuid primary key references public.businesses(id) on delete cascade,
    shopping_advisor_enabled boolean not null default true,
    followups_enabled boolean not null default true,
    followup_delay_hours integer not null default 24 check (followup_delay_hours between 1 and 168),
    loyalty_enabled boolean not null default true,
    points_per_1000 numeric(12,3) not null default 1 check (points_per_1000 >= 0),
    point_value_yer numeric(12,4) not null default 1 check (point_value_yer > 0),
    minimum_redeem_points integer not null default 20 check (minimum_redeem_points >= 1),
    repeat_purchase_threshold integer not null default 3 check (repeat_purchase_threshold between 2 and 100),
    repeat_purchase_bonus_points integer not null default 10 check (repeat_purchase_bonus_points >= 0),
    coupon_expiry_days integer not null default 30 check (coupon_expiry_days between 1 and 365),
    updated_at timestamptz not null default now()
);

alter table public.store_growth_settings
    add column if not exists repeat_purchase_threshold integer not null default 3 check (repeat_purchase_threshold between 2 and 100);
alter table public.store_growth_settings
    add column if not exists repeat_purchase_bonus_points integer not null default 10 check (repeat_purchase_bonus_points >= 0);

insert into public.store_growth_settings (business_id)
select id from public.businesses
on conflict (business_id) do nothing;

create table if not exists public.telegram_customer_preferences (
    business_id uuid not null references public.businesses(id) on delete cascade,
    telegram_chat_id text not null,
    reminders_opt_in boolean not null default false,
    loyalty_opt_in boolean not null default false,
    updated_at timestamptz not null default now(),
    primary key (business_id, telegram_chat_id)
);

create table if not exists public.telegram_customer_followups (
    id uuid primary key default gen_random_uuid(),
    business_id uuid not null references public.businesses(id) on delete cascade,
    telegram_chat_id text not null,
    topic text not null default 'المنتج الذي سألت عنه',
    status text not null default 'pending' check (status in ('pending','sent','cancelled')),
    due_at timestamptz not null,
    created_at timestamptz not null default now(),
    sent_at timestamptz,
    unique (business_id, telegram_chat_id)
);
create index if not exists telegram_customer_followups_due_idx
    on public.telegram_customer_followups (due_at) where status = 'pending';

create table if not exists public.store_loyalty_accounts (
    business_id uuid not null references public.businesses(id) on delete cascade,
    customer_external_id text not null,
    points_balance integer not null default 0 check (points_balance >= 0),
    lifetime_points integer not null default 0 check (lifetime_points >= 0),
    updated_at timestamptz not null default now(),
    primary key (business_id, customer_external_id)
);

create table if not exists public.store_loyalty_ledger (
    id uuid primary key default gen_random_uuid(),
    business_id uuid not null references public.businesses(id) on delete cascade,
    customer_external_id text not null,
    order_id uuid references public.orders(id) on delete set null,
    points_delta integer not null,
    reason text not null,
    created_at timestamptz not null default now()
);
create unique index if not exists store_loyalty_ledger_order_award_unique
    on public.store_loyalty_ledger (order_id) where order_id is not null and points_delta > 0;
create index if not exists store_loyalty_ledger_customer_idx
    on public.store_loyalty_ledger (business_id, customer_external_id, created_at desc);

create table if not exists public.store_coupons (
    id uuid primary key default gen_random_uuid(),
    business_id uuid not null references public.businesses(id) on delete cascade,
    code text not null,
    title text not null default 'مكافأة الولاء',
    points_cost integer not null check (points_cost > 0),
    discount_amount numeric(12,2) not null check (discount_amount > 0),
    currency text not null default 'YER',
    expires_at timestamptz not null,
    max_redemptions integer check (max_redemptions is null or max_redemptions > 0),
    redemption_count integer not null default 0 check (redemption_count >= 0),
    is_active boolean not null default true,
    created_at timestamptz not null default now(),
    unique (business_id, code)
);
create table if not exists public.store_coupon_redemptions (
    id uuid primary key default gen_random_uuid(),
    business_id uuid not null references public.businesses(id) on delete cascade,
    coupon_id uuid not null references public.store_coupons(id) on delete cascade,
    customer_external_id text not null,
    points_spent integer not null,
    redeemed_at timestamptz not null default now(),
    unique (coupon_id, customer_external_id, id)
);

alter table public.store_growth_settings enable row level security;
alter table public.telegram_customer_preferences enable row level security;
alter table public.telegram_customer_followups enable row level security;
alter table public.store_loyalty_accounts enable row level security;
alter table public.store_loyalty_ledger enable row level security;
alter table public.store_coupons enable row level security;
alter table public.store_coupon_redemptions enable row level security;

revoke all on public.store_growth_settings, public.telegram_customer_preferences,
    public.telegram_customer_followups, public.store_loyalty_accounts,
    public.store_loyalty_ledger, public.store_coupons,
    public.store_coupon_redemptions from anon, authenticated;
grant all on public.store_growth_settings, public.telegram_customer_preferences,
    public.telegram_customer_followups, public.store_loyalty_accounts,
    public.store_loyalty_ledger, public.store_coupons,
    public.store_coupon_redemptions to service_role;

create or replace function public.award_store_loyalty_points_on_delivery()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
    v_customer_id text;
    v_points_per_1000 numeric := 1;
    v_enabled boolean := true;
    v_points integer;
    v_repeat_threshold integer := 3;
    v_repeat_bonus integer := 10;
    v_prior_delivered integer := 0;
begin
    if new.status <> 'delivered' or old.status = 'delivered' then
        return new;
    end if;

    v_customer_id := nullif(trim(coalesce(new.telegram_chat_id::text, '')), '');
    if v_customer_id is null then
        return new;
    end if;

    select loyalty_enabled, points_per_1000, repeat_purchase_threshold, repeat_purchase_bonus_points
      into v_enabled, v_points_per_1000, v_repeat_threshold, v_repeat_bonus
      from public.store_growth_settings
     where business_id = new.business_id;

    if not coalesce(v_enabled, true) then
        return new;
    end if;

    if greatest(coalesce(new.total_price, 0), 0) <= 0 then
        return new;
    end if;
    v_points := floor((greatest(coalesce(new.total_price, 0), 0) / 1000.0) * greatest(coalesce(v_points_per_1000, 1), 0))::integer;
    v_repeat_threshold := greatest(coalesce(v_repeat_threshold, 3), 2);
    v_repeat_bonus := greatest(coalesce(v_repeat_bonus, 10), 0);

    select count(*)::integer into v_prior_delivered
      from public.orders
     where business_id = new.business_id
       and telegram_chat_id::text = v_customer_id
       and status = 'delivered'
       and coalesce(total_price, 0) > 0
       and id <> new.id;

    if v_repeat_bonus > 0 and mod(v_prior_delivered + 1, v_repeat_threshold) = 0 then
        v_points := v_points + v_repeat_bonus;
    end if;
    if v_points <= 0 then
        return new;
    end if;

    insert into public.store_loyalty_ledger (business_id, customer_external_id, order_id, points_delta, reason)
    values (new.business_id, v_customer_id, new.id, v_points, 'delivered_order')
    on conflict do nothing;

    if found then
        insert into public.store_loyalty_accounts (business_id, customer_external_id, points_balance, lifetime_points, updated_at)
        values (new.business_id, v_customer_id, v_points, v_points, now())
        on conflict (business_id, customer_external_id) do update
          set points_balance = public.store_loyalty_accounts.points_balance + excluded.points_balance,
              lifetime_points = public.store_loyalty_accounts.lifetime_points + excluded.lifetime_points,
              updated_at = now();
    end if;

    return new;
end;
$$;

drop trigger if exists trg_award_store_loyalty_points_on_delivery on public.orders;
create trigger trg_award_store_loyalty_points_on_delivery
after update of status on public.orders
for each row
when (new.status = 'delivered' and old.status is distinct from new.status)
execute function public.award_store_loyalty_points_on_delivery();

create or replace function public.redeem_store_loyalty_coupon(
    p_business_id uuid,
    p_customer_external_id text,
    p_coupon_code text
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_coupon public.store_coupons%rowtype;
    v_account public.store_loyalty_accounts%rowtype;
    v_loyalty_enabled boolean := true;
    v_minimum_points integer := 20;
begin
    select loyalty_enabled, minimum_redeem_points
      into v_loyalty_enabled, v_minimum_points
      from public.store_growth_settings
     where business_id = p_business_id;
    if not coalesce(v_loyalty_enabled, true) then
        return jsonb_build_object('success', false, 'reason', 'coupon_unavailable');
    end if;

    select * into v_coupon
      from public.store_coupons
     where business_id = p_business_id
       and upper(code) = upper(trim(p_coupon_code))
       and is_active = true
       and expires_at > now()
       and (max_redemptions is null or redemption_count < max_redemptions)
     for update;

    if not found or v_coupon.points_cost < greatest(coalesce(v_minimum_points, 20), 1) then
        return jsonb_build_object('success', false, 'reason', 'coupon_unavailable');
    end if;

    select * into v_account
      from public.store_loyalty_accounts
     where business_id = p_business_id
       and customer_external_id = p_customer_external_id
     for update;

    if not found or v_account.points_balance < v_coupon.points_cost then
        return jsonb_build_object('success', false, 'reason', 'insufficient_points');
    end if;

    update public.store_loyalty_accounts
       set points_balance = points_balance - v_coupon.points_cost,
           updated_at = now()
     where business_id = p_business_id
       and customer_external_id = p_customer_external_id;

    insert into public.store_loyalty_ledger (business_id, customer_external_id, points_delta, reason)
    values (p_business_id, p_customer_external_id, -v_coupon.points_cost, 'coupon:' || v_coupon.code);

    insert into public.store_coupon_redemptions (business_id, coupon_id, customer_external_id, points_spent)
    values (p_business_id, v_coupon.id, p_customer_external_id, v_coupon.points_cost);

    update public.store_coupons
       set redemption_count = redemption_count + 1
     where id = v_coupon.id;

    return jsonb_build_object(
        'success', true,
        'code', v_coupon.code,
        'title', v_coupon.title,
        'discount_amount', v_coupon.discount_amount,
        'currency', v_coupon.currency,
        'expires_at', v_coupon.expires_at
    );
end;
$$;

revoke all on function public.redeem_store_loyalty_coupon(uuid, text, text) from public, anon, authenticated;
grant execute on function public.redeem_store_loyalty_coupon(uuid, text, text) to service_role;
