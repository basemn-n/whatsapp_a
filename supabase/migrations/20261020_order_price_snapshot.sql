-- Run this once in Supabase SQL Editor before relying on exact sales totals.
-- Existing orders are intentionally left without a historical price snapshot;
-- the admin report will label those amounts as estimates using the current catalog price.
ALTER TABLE public.orders
  ADD COLUMN IF NOT EXISTS unit_price numeric(12, 2),
  ADD COLUMN IF NOT EXISTS total_price numeric(12, 2),
  ADD COLUMN IF NOT EXISTS currency text;

CREATE INDEX IF NOT EXISTS idx_orders_business_created_at
  ON public.orders (business_id, created_at DESC);

COMMENT ON COLUMN public.orders.unit_price IS
  'Catalog unit price captured when the customer order was created.';
COMMENT ON COLUMN public.orders.total_price IS
  'Unit price multiplied by quantity, captured when the order was created.';
COMMENT ON COLUMN public.orders.currency IS
  'Currency captured when the customer order was created.';
