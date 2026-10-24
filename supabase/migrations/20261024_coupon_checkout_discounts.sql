-- One-time customer coupon codes and atomic discount application at Telegram checkout.
-- Apply this migration in Supabase SQL Editor before deploying the matching app release.

ALTER TABLE public.store_coupon_redemptions
    ADD COLUMN IF NOT EXISTS redemption_code text,
    ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'issued'
        CHECK (status IN ('issued', 'used', 'void')),
    ADD COLUMN IF NOT EXISTS discount_amount numeric(12,2),
    ADD COLUMN IF NOT EXISTS currency text NOT NULL DEFAULT 'YER',
    ADD COLUMN IF NOT EXISTS expires_at timestamptz,
    ADD COLUMN IF NOT EXISTS order_id uuid REFERENCES public.orders(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS used_at timestamptz;

-- Historical redemptions have no issued customer code; keep them consumed, not redeemable.
UPDATE public.store_coupon_redemptions
   SET redemption_code = 'LEGACY-' || upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 12)),
       status = 'used',
       discount_amount = COALESCE(discount_amount, 0),
       expires_at = COALESCE(expires_at, redeemed_at)
 WHERE redemption_code IS NULL;

ALTER TABLE public.orders
    ADD COLUMN IF NOT EXISTS subtotal_price numeric(12,2),
    ADD COLUMN IF NOT EXISTS discount_amount numeric(12,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS discount_code text,
    ADD COLUMN IF NOT EXISTS coupon_redemption_id uuid;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'orders_coupon_redemption_id_fkey'
           AND conrelid = 'public.orders'::regclass
    ) THEN
        ALTER TABLE public.orders
            ADD CONSTRAINT orders_coupon_redemption_id_fkey
            FOREIGN KEY (coupon_redemption_id)
            REFERENCES public.store_coupon_redemptions(id)
            ON DELETE SET NULL;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS store_coupon_redemptions_code_unique
    ON public.store_coupon_redemptions (redemption_code)
    WHERE redemption_code IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS orders_coupon_redemption_unique
    ON public.orders (coupon_redemption_id)
    WHERE coupon_redemption_id IS NOT NULL;

COMMENT ON COLUMN public.orders.subtotal_price IS
    'Order amount before loyalty coupon discount.';
COMMENT ON COLUMN public.orders.discount_amount IS
    'Loyalty coupon discount applied atomically at checkout.';
COMMENT ON COLUMN public.orders.total_price IS
    'Final order amount after discount, saved atomically at checkout.';
COMMENT ON COLUMN public.store_coupon_redemptions.redemption_code IS
    'Unique customer-specific one-time code issued after loyalty points are redeemed.';

CREATE OR REPLACE FUNCTION public.redeem_store_loyalty_coupon(
    p_business_id uuid,
    p_customer_external_id text,
    p_coupon_code text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_coupon public.store_coupons%ROWTYPE;
    v_account public.store_loyalty_accounts%ROWTYPE;
    v_redemption_code text;
    v_loyalty_enabled boolean := true;
    v_minimum_points integer := 20;
BEGIN
    SELECT loyalty_enabled, minimum_redeem_points
      INTO v_loyalty_enabled, v_minimum_points
      FROM public.store_growth_settings
     WHERE business_id = p_business_id;
    IF NOT COALESCE(v_loyalty_enabled, true) THEN
        RETURN jsonb_build_object('success', false, 'reason', 'coupon_unavailable');
    END IF;

    SELECT * INTO v_coupon
      FROM public.store_coupons
     WHERE business_id = p_business_id
       AND upper(code) = upper(trim(p_coupon_code))
       AND is_active = true
       AND expires_at > now()
       AND (max_redemptions IS NULL OR redemption_count < max_redemptions)
     FOR UPDATE;

    IF NOT FOUND OR v_coupon.points_cost < GREATEST(COALESCE(v_minimum_points, 20), 1) THEN
        RETURN jsonb_build_object('success', false, 'reason', 'coupon_unavailable');
    END IF;

    SELECT * INTO v_account
      FROM public.store_loyalty_accounts
     WHERE business_id = p_business_id
       AND customer_external_id = p_customer_external_id
     FOR UPDATE;

    IF NOT FOUND OR v_account.points_balance < v_coupon.points_cost THEN
        RETURN jsonb_build_object('success', false, 'reason', 'insufficient_points');
    END IF;

    v_redemption_code := 'LOY-' || upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 12));

    UPDATE public.store_loyalty_accounts
       SET points_balance = points_balance - v_coupon.points_cost,
           updated_at = now()
     WHERE business_id = p_business_id
       AND customer_external_id = p_customer_external_id;

    INSERT INTO public.store_loyalty_ledger (business_id, customer_external_id, points_delta, reason)
    VALUES (p_business_id, p_customer_external_id, -v_coupon.points_cost, 'coupon:' || v_coupon.code);

    INSERT INTO public.store_coupon_redemptions (
        business_id, coupon_id, customer_external_id, points_spent,
        redemption_code, status, discount_amount, currency, expires_at
    ) VALUES (
        p_business_id, v_coupon.id, p_customer_external_id, v_coupon.points_cost,
        v_redemption_code, 'issued', v_coupon.discount_amount, v_coupon.currency, v_coupon.expires_at
    );

    UPDATE public.store_coupons
       SET redemption_count = redemption_count + 1
     WHERE id = v_coupon.id;

    RETURN jsonb_build_object(
        'success', true,
        'code', v_coupon.code,
        'redemption_code', v_redemption_code,
        'title', v_coupon.title,
        'discount_amount', v_coupon.discount_amount,
        'currency', v_coupon.currency,
        'expires_at', v_coupon.expires_at
    );
END;
$$;

REVOKE ALL ON FUNCTION public.redeem_store_loyalty_coupon(uuid, text, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.redeem_store_loyalty_coupon(uuid, text, text)
    TO service_role;

-- Replace the previous eight-argument function so the optional ninth argument is unambiguous.
DROP FUNCTION IF EXISTS public.create_telegram_order_with_stock(
    uuid, text, text, text, text, uuid, integer, text
);

CREATE OR REPLACE FUNCTION public.create_telegram_order_with_stock(
    p_business_id uuid,
    p_customer_name text,
    p_customer_phone text,
    p_customer_address text,
    p_payment_method text,
    p_product_id uuid,
    p_quantity integer,
    p_telegram_chat_id text,
    p_redemption_code text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $function$
DECLARE
    v_product public.products%ROWTYPE;
    v_order public.orders%ROWTYPE;
    v_redemption public.store_coupon_redemptions%ROWTYPE;
    v_subtotal numeric(12,2);
    v_discount numeric(12,2) := 0;
    v_total numeric(12,2);
    v_currency text;
    v_redemption_id uuid;
    v_discount_code text;
BEGIN
    IF p_quantity IS NULL OR p_quantity < 1 OR p_quantity > 1000 THEN
        RETURN jsonb_build_object('success', false, 'reason', 'invalid_quantity');
    END IF;

    IF p_business_id IS NULL
       OR p_product_id IS NULL
       OR NULLIF(trim(p_customer_name), '') IS NULL
       OR NULLIF(trim(p_customer_phone), '') IS NULL
       OR NULLIF(trim(p_customer_address), '') IS NULL
       OR NULLIF(trim(p_payment_method), '') IS NULL
       OR NULLIF(trim(p_telegram_chat_id), '') IS NULL THEN
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

    v_currency := COALESCE(NULLIF(v_product.currency, ''), 'YER');

    IF NULLIF(trim(p_redemption_code), '') IS NOT NULL THEN
        SELECT *
          INTO v_redemption
          FROM public.store_coupon_redemptions
         WHERE business_id = p_business_id
           AND customer_external_id = p_telegram_chat_id
           AND upper(redemption_code) = upper(trim(p_redemption_code))
           AND status = 'issued'
           AND expires_at > now()
         FOR UPDATE;

        IF NOT FOUND THEN
            RETURN jsonb_build_object('success', false, 'reason', 'coupon_invalid');
        END IF;

        IF upper(COALESCE(v_redemption.currency, 'YER')) <> upper(v_currency) THEN
            RETURN jsonb_build_object('success', false, 'reason', 'coupon_currency_mismatch');
        END IF;

        v_discount := GREATEST(COALESCE(v_redemption.discount_amount, 0), 0);
        v_redemption_id := v_redemption.id;
        v_discount_code := v_redemption.redemption_code;
    END IF;

    IF COALESCE(v_product.stock_quantity, 0) < p_quantity THEN
        RETURN jsonb_build_object(
            'success', false,
            'reason', 'insufficient_stock',
            'available_stock', COALESCE(v_product.stock_quantity, 0)
        );
    END IF;

    v_subtotal := round(COALESCE(v_product.price, 0) * p_quantity, 2);
    v_discount := LEAST(v_discount, v_subtotal);
    v_total := GREATEST(0, round(v_subtotal - v_discount, 2));

    UPDATE public.products
       SET stock_quantity = stock_quantity - p_quantity
     WHERE id = v_product.id
       AND business_id = p_business_id;

    INSERT INTO public.orders (
        business_id, product_id, customer_name, customer_phone,
        customer_address, payment_method, product_name, quantity,
        status, telegram_chat_id, unit_price, subtotal_price,
        discount_amount, discount_code, coupon_redemption_id, total_price, currency
    ) VALUES (
        p_business_id, v_product.id, trim(p_customer_name),
        trim(p_customer_phone), trim(p_customer_address),
        trim(p_payment_method), v_product.name, p_quantity, 'pending',
        p_telegram_chat_id, v_product.price, v_subtotal,
        v_discount, v_discount_code, v_redemption_id, v_total, v_currency
    )
    RETURNING * INTO v_order;

    IF v_redemption_id IS NOT NULL THEN
        UPDATE public.store_coupon_redemptions
           SET status = 'used', order_id = v_order.id, used_at = now()
         WHERE id = v_redemption_id
           AND status = 'issued';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Coupon redemption was concurrently consumed';
        END IF;
    END IF;

    RETURN jsonb_build_object(
        'success', true,
        'order', jsonb_build_object(
            'id', v_order.id,
            'order_number', v_order.order_number,
            'status', v_order.status,
            'subtotal_price', v_order.subtotal_price,
            'discount_amount', v_order.discount_amount,
            'discount_code', v_order.discount_code,
            'total_price', v_order.total_price,
            'currency', v_order.currency
        ),
        'remaining_stock', v_product.stock_quantity - p_quantity
    );
END;
$function$;

REVOKE ALL ON FUNCTION public.create_telegram_order_with_stock(
    uuid, text, text, text, text, uuid, integer, text, text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.create_telegram_order_with_stock(
    uuid, text, text, text, text, uuid, integer, text, text
) TO service_role;
