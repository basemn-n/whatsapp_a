"""Safe, tenant-scoped post-processing for AI visual product search."""
from __future__ import annotations


def select_visual_product_matches(products, model_result, min_confidence=55, limit=3):
    """Validate model-suggested product IDs against the supplied store catalog.

    The model never supplies authoritative prices or availability. Those fields
    always come from the tenant-scoped product rows passed into this function.
    """
    if not isinstance(products, list) or not isinstance(model_result, dict):
        return []

    by_id = {
        str(product.get("id")): product
        for product in products
        if isinstance(product, dict) and product.get("id") is not None
    }
    raw_matches = model_result.get("matches")
    if not isinstance(raw_matches, list):
        return []

    selected = []
    seen = set()
    for match in raw_matches:
        if not isinstance(match, dict):
            continue
        product_id = str(match.get("product_id") or "")
        if not product_id or product_id in seen or product_id not in by_id:
            continue
        product = by_id[product_id]
        if not product.get("is_available", False):
            continue
        stock = product.get("stock_quantity")
        if stock is not None:
            try:
                if int(stock) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
        try:
            confidence = int(match.get("confidence", 0))
        except (TypeError, ValueError):
            continue
        confidence = max(0, min(100, confidence))
        if confidence < min_confidence:
            continue
        seen.add(product_id)
        selected.append({
            "product": product,
            "confidence": confidence,
            "reason": str(match.get("reason") or "")[:240],
        })

    selected.sort(key=lambda item: item["confidence"], reverse=True)
    return selected[:max(1, min(int(limit), 5))]
