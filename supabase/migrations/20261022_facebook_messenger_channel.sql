-- Enable Facebook Messenger tenant mappings without changing existing connections.
alter table public.channel_connections
    drop constraint if exists channel_connections_channel_check;

alter table public.channel_connections
    add constraint channel_connections_channel_check
    check (channel in ('telegram_products', 'whatsapp', 'telegram', 'facebook_messenger'));
