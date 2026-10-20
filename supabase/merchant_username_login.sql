-- Update the existing merchant_accounts table to use username instead of email.
-- Safe for both installations of the earlier email-based schema and fresh username schema.
do $$
begin
    if exists (
        select 1 from information_schema.columns
        where table_schema = 'public'
          and table_name = 'merchant_accounts'
          and column_name = 'email'
    ) and not exists (
        select 1 from information_schema.columns
        where table_schema = 'public'
          and table_name = 'merchant_accounts'
          and column_name = 'username'
    ) then
        alter table public.merchant_accounts rename column email to username;
    end if;
end $$;

create unique index if not exists merchant_accounts_username_unique_idx
    on public.merchant_accounts (username);
