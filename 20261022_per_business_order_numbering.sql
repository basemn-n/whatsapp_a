-- Per-business order numbering.
-- Run once in Supabase SQL Editor. This migration preserves order rows and stock values.
BEGIN;

LOCK TABLE public.orders IN ACCESS EXCLUSIVE MODE;

-- A merchant's order numbers are unique within that merchant, not globally.
ALTER TABLE public.orders
    DROP CONSTRAINT IF EXISTS orders_order_number_key;

-- Stop the global identity sequence from assigning order numbers.
ALTER TABLE public.orders
    ALTER COLUMN order_number DROP IDENTITY IF EXISTS;

-- Re-number historical orders from 1 within each business, oldest first.
WITH numbered AS (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY business_id
            ORDER BY created_at ASC NULLS LAST, id ASC
        )::bigint AS new_order_number
    FROM public.orders
    WHERE business_id IS NOT NULL
)
UPDATE public.orders AS o
SET order_number = numbered.new_order_number
FROM numbered
WHERE o.id = numbered.id
  AND o.order_number IS DISTINCT FROM numbered.new_order_number;

-- Each business has its own next-number counter.
CREATE TABLE IF NOT EXISTS public.business_order_counters (
    business_id uuid PRIMARY KEY REFERENCES public.businesses(id) ON DELETE CASCADE,
    next_order_number bigint NOT NULL CHECK (next_order_number >= 1)
);

-- Seed counters from the current highest order number for each business.
INSERT INTO public.business_order_counters (business_id, next_order_number)
SELECT business_id, COALESCE(MAX(order_number), 0) + 1
FROM public.orders
WHERE business_id IS NOT NULL
GROUP BY business_id
ON CONFLICT (business_id) DO UPDATE
SET next_order_number = EXCLUDED.next_order_number;

CREATE UNIQUE INDEX IF NOT EXISTS orders_business_id_order_number_key
    ON public.orders (business_id, order_number);

-- Current RPC: reserve stock and allocate a per-business order number atomically.
CREATE OR REPLACE FUNCTION public.create_telegram_order_with_stock(
    p_business_id uuid,
    p_customer_name text,
    p_customer_phone text,
    p_customer_address text,
    p_payment_method text,
    p_product_id uuid,
    p_quantity integer,
    p_telegram_chat_id text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $function$
DECLARE
    v_product public.products%ROWTYPE;
    v_order public.orders%ROWTYPE;
    v_order_number bigint;
BEGIN
    IF p_quantity IS NULL OR p_quantity < 1 OR p_quantity > 1000 THEN
        RETURN jsonb_build_object('success', false, 'reason', 'invalid_quantity');
    END IF;

    IF p_business_id IS NULL
       OR p_product_id IS NULL
       OR NULLIF(trim(p_customer_name), '') IS NULL
       OR NULLIF(trim(p_customer_phone), '') IS NULL
       OR NULLIF(trim(p_customer_address), '') IS NULL
       OR NULLIF(trim(p_payment_method), '') IS NULL THEN
        RETURN jsonb_build_object('success', false, 'reason', 'missing_fields');
    END IF;

    SELECT *
      INTO v_product
      FROM public.products
     WHERE id = p_product_id
       AND business_id = p_business_id
     FOR UPDATE;

    IF NOT FOUND OR NOT v_product.is_available THEN
        RETURN jsonb_build_object('success', false, 'reason', 'product_unavailable');
    END IF;

    IF COALESCE(v_product.stock_quantity, 0) < p_quantity THEN
        RETURN jsonb_build_object(
            'success', false,
            'reason', 'insufficient_stock',
            'available_stock', COALESCE(v_product.stock_quantity, 0)
        );
    END IF;

    -- Atomic per-business sequence. A new business starts at order 1.
    INSERT INTO public.business_order_counters (business_id, next_order_number)
    VALUES (p_business_id, 2)
    ON CONFLICT (business_id) DO UPDATE
       SET next_order_number = public.business_order_counters.next_order_number + 1
    RETURNING next_order_number - 1 INTO v_order_number;

    UPDATE public.products
       SET stock_quantity = stock_quantity - p_quantity
     WHERE id = v_product.id
       AND business_id = p_business_id;

    INSERT INTO public.orders (
        business_id, order_number, product_id, customer_name, customer_phone,
        customer_address, payment_method, product_name, quantity,
        status, telegram_chat_id, unit_price, total_price, currency
    ) VALUES (
        p_business_id, v_order_number, v_product.id, trim(p_customer_name),
        trim(p_customer_phone), trim(p_customer_address),
        trim(p_payment_method), v_product.name, p_quantity, 'pending',
        p_telegram_chat_id, v_product.price,
        round(COALESCE(v_product.price, 0) * p_quantity, 2),
        COALESCE(NULLIF(v_product.currency, ''), 'YER')
    )
    RETURNING * INTO v_order;

    RETURN jsonb_build_object(
        'success', true,
        'order', jsonb_build_object(
            'id', v_order.id,
            'order_number', v_order.order_number,
            'status', v_order.status
        ),
        'remaining_stock', v_product.stock_quantity - p_quantity
    );
END;
$function$;

REVOKE ALL ON FUNCTION public.create_telegram_order_with_stock(
    uuid, text, text, text, text, uuid, integer, text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.create_telegram_order_with_stock(
    uuid, text, text, text, text, uuid, integer, text
) TO service_role;

-- Compatibility wrapper for the legacy RPC signature that accepts a product name.
CREATE OR REPLACE FUNCTION public.create_telegram_order_with_stock(
    p_business_id uuid,
    p_customer_name text,
    p_customer_phone text,
    p_customer_address text,
    p_payment_method text,
    p_product_name text,
    p_quantity integer,
    p_telegram_chat_id text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $function$
DECLARE
    v_product_id uuid;
BEGIN
    SELECT id
      INTO v_product_id
      FROM public.products
     WHERE business_id = p_business_id
       AND name = p_product_name
       AND is_available = true
     ORDER BY created_at ASC NULLS LAST, id ASC
     LIMIT 1;

    IF v_product_id IS NULL THEN
        RETURN jsonb_build_object('success', false, 'reason', 'product_unavailable');
    END IF;

    RETURN public.create_telegram_order_with_stock(
        p_business_id,
        p_customer_name,
        p_customer_phone,
        p_customer_address,
        p_payment_method,
        v_product_id,
        p_quantity,
        p_telegram_chat_id
    );
END;
$function$;

REVOKE ALL ON FUNCTION public.create_telegram_order_with_stock(
    uuid, text, text, text, text, text, integer, text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.create_telegram_order_with_stock(
    uuid, text, text, text, text, text, integer, text
) TO service_role;

COMMIT;
