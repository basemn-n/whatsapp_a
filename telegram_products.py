""""Import product posts from a configured Telegram channel into Supabase."""

import logging
import re

logger = logging.getLogger(__name__)

_DIGIT_TRANSLATION = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)

_MODEL_RE = re.compile(
    r"(?:رقم\\s*الموديل|الموديل|موديل|model(?:\\s*(?:number|no\\.?))?|sku)"
    r"\\s*[:：#\\-]?\\s*([A-Za-z0-9٠-٩۰-۹][A-Za-z0-9٠-٩۰-۹/_-]*)",
    re.IGNORECASE,
)
_PRICE_LABEL_RE = re.compile(
    r"(?:السعر|price|بمبلغ)\\s*[:：\\-]?\\s*"
    r"([0-9٠-٩۰-۹][0-9٠-٩۰-۹,٬٫.]*)",
    re.IGNORECASE,
)
_CURRENCY_PRICE_RE = re.compile(
    r"([0-9٠-٩۰-۹][0-9٠-٩۰-۹,٬٫.]*)\\s*"
    r"(ريال(?:\\s*(?:سعودي|يمني))?|ر\\.?\\s*س|SAR|YER|USD|دولار)",
    re.IGNORECASE,
)


def _normalise_digits(value: str) -> str:
    return str(value or "").translate(_DIGIT_TRANSLATION)


def _parse_amount(value: str):
    value = _normalise_digits(value).replace("٬", "").replace(",", "").replace("٫", ".")
    if value.count(".") > 1:
        parts = value.split(".")
        value = "".join(parts[:-1]) + "." + parts[-1]
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount < 0 or amount > 1_000_000_000:
        return None
    return round(amount, 2)


def _currency_from_text(text: str, default_currency: str) -> str:
    lowered = text.casefold()
    if re.search(r"\\bSAR\\b|ر\\.?\\s*س|ريال\\s*سعودي", lowered, re.IGNORECASE):
        return "SAR"
    if re.search(r"\\bUSD\\b|دولار", lowered, re.IGNORECASE):
        return "USD"
    if re.search(r"\\bYER\\b|ريال\\s*يمني", lowered, re.IGNORECASE):
        return "YER"
    return (default_currency or "YER").upper()


def _extract_fields(post: dict, default_currency: str):
    text = (post.get("caption") or post.get("text") or "").strip()
    if not text:
        return None

    model_match = _MODEL_RE.search(text)
    model = _normalise_digits(model_match.group(1)) if model_match else ""

    price_match = _PRICE_LABEL_RE.search(text)
    if price_match:
        price = _parse_amount(price_match.group(1))
    else:
        currency_match = _CURRENCY_PRICE_RE.search(text)
        price = _parse_amount(currency_match.group(1)) if currency_match else None

    # Never guess a price from a model number or from the image.
    if price is None:
        return {"error": "no_price", "text": text[:2000]}

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    title = ""
    for line in lines:
        if re.match(r"^(?:السعر|price|رقم\\s*الموديل|الموديل|موديل|sku)\\s*[:：\\-]?", line, re.IGNORECASE):
            continue
        title = line
        break

    if not title:
        title = lines[0] if lines else ""

    title = re.sub(
        r"(?:السعر|price|بمبلغ)\\s*[:：\\-]?\\s*"
        r"[0-9٠-٩۰-۹][0-9٠-٩۰-۹,٬٫.]*.*$",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip(" \\t-–—:؛,")
    title = re.sub(
        r"\\s*[0-9٠-٩۰-۹][0-9٠-٩۰-۹,٬٫.]*\\s*"
        r"(?:ريال(?:\\s*(?:سعودي|يمني))?|ر\\.?\\s*س|SAR|YER|USD|دولار)\\s*$",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip(" \\t-–—:؛,")
    if not title:
        return {"error": "no_name", "text": text[:2000]}

    if model and model.casefold() not in title.casefold():
        title = f"{title} موديل {model}"

    currency = _currency_from_text(text, default_currency)
    description_lines = []
    if model:
        description_lines.append(f"رقم الموديل: {model}")
    description_lines.extend(["نص منشور تيليجرام:", text])

    return {
        "name": title[:200],
        "description": "\\n".join(description_lines)[:2000],
        "price": price,
        "currency": currency,
        "model_number": model or None,
        "is_available": True,
    }


def import_telegram_product_post(post: dict, supabase, business_id: str, default_currency: str = "YER"):
    """Create or update a product from a channel post; never cross tenant boundaries."""
    if supabase is None:
        return "skipped_supabase_not_configured"
    if not business_id:
        return "skipped_business_id_missing"

    fields = _extract_fields(post, default_currency)
    if not fields:
        return "skipped_empty_post"
    if fields.get("error"):
        logger.warning(
            "Skipping Telegram product post: missing %s; message_id=%s",
            fields["error"], post.get("message_id"),
        )
        return f"skipped_{fields['error']}"

    business = (
        supabase.table("businesses").select("id")
        .eq("id", business_id).limit(1).execute()
    )
    if not business.data:
        return "skipped_business_not_found"

    # Match by model number first so a renamed post updates the same SKU.
    existing = None
    if fields.get("model_number"):
        result = (
            supabase.table("products").select("id")
            .eq("business_id", business_id)
            .eq("model_number", fields["model_number"])
            .limit(1).execute()
        )
        existing = result.data[0] if result.data else None

    if not existing:
        result = (
            supabase.table("products").select("id")
            .eq("business_id", business_id)
            .eq("name", fields["name"])
            .limit(1).execute()
        )
        existing = result.data[0] if result.data else None

    if existing:
        (
            supabase.table("products").update({
                "name": fields["name"],
                "description": fields["description"],
                "price": fields["price"],
                "currency": fields["currency"],
                "model_number": fields.get("model_number"),
                "is_available": fields["is_available"],
            })
            .eq("id", existing["id"])
            .eq("business_id", business_id).execute()
        )
        return f"updated:{fields['name']}"

    supabase.table("products").insert({"business_id": business_id, **fields}).execute()
    return f"inserted:{fields['name']}"
