-- Atomically reserve merchant stock and create a Telegram order.
-- Execute through the application's service-role Supabase client only.
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
