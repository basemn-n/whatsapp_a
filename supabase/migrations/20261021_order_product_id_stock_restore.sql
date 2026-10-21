-- Keep stock restoration tied to the exact product ordered.
-- Existing orders are preserved; product_id remains NULL for historical orders
-- until they can be linked safely.
ALTER TABLE public.orders
    ADD COLUMN IF NOT EXISTS product_id uuid;

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'orders_product_id_fkey'
          AND conrelid = 'public.orders'::regclass
    ) THEN
        ALTER TABLE public.orders
            ADD CONSTRAINT orders_product_id_fkey
            FOREIGN KEY (product_id)
            REFERENCES public.products(id)
            ON DELETE SET NULL;
    END IF;
END;
$migration$;

CREATE INDEX IF NOT EXISTS idx_orders_business_product_id
    ON public.orders (business_id, product_id);

-- Future orders store the product's immutable ID alongside the display-name snapshot.
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
    v_product public.products%ROWTYPE;
    v_order public.orders%ROWTYPE;
BEGIN
    IF p_quantity IS NULL OR p_quantity < 1 OR p_quantity > 1000 THEN
        RETURN jsonb_build_object('success', false, 'reason', 'invalid_quantity');
    END IF;

    IF p_business_id IS NULL
       OR NULLIF(trim(p_customer_name), '') IS NULL
       OR NULLIF(trim(p_customer_phone), '') IS NULL
       OR NULLIF(trim(p_customer_address), '') IS NULL
       OR NULLIF(trim(p_payment_method), '') IS NULL
       OR NULLIF(trim(p_product_name), '') IS NULL THEN
        RETURN jsonb_build_object('success', false, 'reason', 'missing_fields');
    END IF;

    SELECT *
      INTO v_product
      FROM public.products
     WHERE business_id = p_business_id
       AND name = p_product_name
     LIMIT 1
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

    UPDATE public.products
       SET stock_quantity = stock_quantity - p_quantity
     WHERE id = v_product.id
       AND business_id = p_business_id;

    INSERT INTO public.orders (
        business_id,
        product_id,
        customer_name,
        customer_phone,
        customer_address,
        payment_method,
        product_name,
        quantity,
        status,
        telegram_chat_id,
        unit_price,
        total_price,
        currency
    ) VALUES (
        p_business_id,
        v_product.id,
        trim(p_customer_name),
        trim(p_customer_phone),
        trim(p_customer_address),
        trim(p_payment_method),
        v_product.name,
        p_quantity,
        'pending',
        p_telegram_chat_id,
        v_product.price,
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
    uuid, text, text, text, text, text, integer, text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.create_telegram_order_with_stock(
    uuid, text, text, text, text, text, integer, text
) TO service_role;

-- Restore stock by product_id for new orders. Legacy orders without a product_id
-- are restored by name only when exactly one matching product exists.
CREATE OR REPLACE FUNCTION public.update_telegram_order_status_with_stock(
    p_business_id uuid,
    p_order_id uuid,
    p_new_status text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $function$
DECLARE
    v_order public.orders%ROWTYPE;
    v_product public.products%ROWTYPE;
    v_matching_products integer;
BEGIN
    IF p_new_status NOT IN ('pending', 'approved', 'processing', 'shipped', 'delivered', 'cancelled') THEN
        RETURN jsonb_build_object('success', false, 'reason', 'invalid_status');
    END IF;

    SELECT *
      INTO v_order
      FROM public.orders
     WHERE id = p_order_id
       AND business_id = p_business_id
     FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('success', false, 'reason', 'order_not_found');
    END IF;

    IF v_order.status = p_new_status THEN
        RETURN jsonb_build_object('success', true, 'already_applied', true, 'order', to_jsonb(v_order));
    END IF;

    IF v_order.status IN ('shipped', 'delivered') AND p_new_status = 'cancelled' THEN
        RETURN jsonb_build_object('success', false, 'reason', 'cannot_cancel_after_shipping');
    END IF;

    IF v_order.status IN ('delivered', 'cancelled') THEN
        RETURN jsonb_build_object('success', false, 'reason', 'terminal_status');
    END IF;

    IF p_new_status = 'cancelled' THEN
        IF v_order.product_id IS NOT NULL THEN
            SELECT *
              INTO v_product
              FROM public.products
             WHERE id = v_order.product_id
               AND business_id = p_business_id
             FOR UPDATE;

            IF NOT FOUND THEN
                RETURN jsonb_build_object('success', false, 'reason', 'product_not_found');
            END IF;
        ELSE
            SELECT count(*)
              INTO v_matching_products
              FROM public.products
             WHERE business_id = p_business_id
               AND name = v_order.product_name;

            IF v_matching_products <> 1 THEN
                RETURN jsonb_build_object(
                    'success', false,
                    'reason', CASE
                        WHEN v_matching_products = 0 THEN 'product_not_found'
                        ELSE 'ambiguous_product'
                    END
                );
            END IF;

            SELECT *
              INTO v_product
              FROM public.products
             WHERE business_id = p_business_id
               AND name = v_order.product_name
             FOR UPDATE;
        END IF;

        UPDATE public.products
           SET stock_quantity = COALESCE(stock_quantity, 0)
                               + GREATEST(COALESCE(v_order.quantity, 0), 0)
         WHERE id = v_product.id
           AND business_id = p_business_id;
    END IF;

    UPDATE public.orders
       SET status = p_new_status
     WHERE id = p_order_id
       AND business_id = p_business_id
    RETURNING * INTO v_order;

    RETURN jsonb_build_object(
        'success', true,
        'already_applied', false,
        'order', to_jsonb(v_order)
    );
END;
$function$;

REVOKE ALL ON FUNCTION public.update_telegram_order_status_with_stock(uuid, uuid, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.update_telegram_order_status_with_stock(uuid, uuid, text)
    TO service_role;
