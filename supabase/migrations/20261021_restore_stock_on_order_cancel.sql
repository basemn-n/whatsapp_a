-- Update order status and restore reserved stock exactly once on cancellation.
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

    IF p_new_status = 'cancelled' AND v_order.status <> 'cancelled' THEN
        SELECT *
          INTO v_product
          FROM public.products
         WHERE business_id = p_business_id
           AND name = v_order.product_name
         LIMIT 1
         FOR UPDATE;

        IF NOT FOUND THEN
            RETURN jsonb_build_object('success', false, 'reason', 'product_not_found');
        END IF;

        UPDATE public.products
           SET stock_quantity = COALESCE(stock_quantity, 0) + GREATEST(COALESCE(v_order.quantity, 0), 0)
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
