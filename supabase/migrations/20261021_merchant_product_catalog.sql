-- Additive product catalog fields. Existing products and business ownership remain unchanged.
ALTER TABLE public.products
  ADD COLUMN IF NOT EXISTS model_number text NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS category text NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS image_url text NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS stock_quantity integer NOT NULL DEFAULT 0;

DO $
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'products_stock_quantity_nonnegative'
      AND conrelid = 'public.products'::regclass
  ) THEN
    ALTER TABLE public.products
      ADD CONSTRAINT products_stock_quantity_nonnegative
      CHECK (stock_quantity >= 0) NOT VALID;
  END IF;
END $;

CREATE INDEX IF NOT EXISTS idx_products_business_category
  ON public.products (business_id, category);

COMMENT ON COLUMN public.products.model_number IS 'Merchant-entered model number or SKU.';
COMMENT ON COLUMN public.products.category IS 'Merchant-defined product category.';
COMMENT ON COLUMN public.products.image_url IS 'Optional public HTTPS image URL for product display.';
COMMENT ON COLUMN public.products.stock_quantity IS 'Merchant-maintained available stock count.';
