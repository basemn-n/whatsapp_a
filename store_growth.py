"""Small, deterministic helpers for tenant-scoped shopping and retention flows."""
import re


SHOPPING_ADVICE_MARKERS = (
    "هدية", "هديه", "ميزانية", "ميزانيتي", "أنسب", "انسب", "ترشح",
    "ترشيح", "محتار", "محتارة", "قارن", "مقارنة", "أفضل منتج",
    "افضل منتج", "ماذا أشتري", "ماذا اشتري", "أريد شيء", "اريد شيء",
    "أبغى شيء", "ابغى شيء", "اختار لي", "اختاري لي", "يناسبني",
    "مناسب لي", "بحدود", "في حدود", "للاستخدام", "للاستخدام اليومي",
)


def is_shopping_advice_request(message: str) -> bool:
    text = " ".join(str(message or "").lower().split())
    return bool(text) and any(marker in text for marker in SHOPPING_ADVICE_MARKERS)


def is_product_inquiry(message: str) -> bool:
    text = " ".join(str(message or "").lower().split())
    if not text or len(text) < 4:
        return False
    markers = (
        "سعر", "بكم", "كم", "متوفر", "متوفرة", "عندكم", "أريد", "اريد",
        "أبغى", "ابغى", "تفاصيل", "مواصفات", "لون", "موديل", "منتج",
        "هدية", "ميزانية", "ترشح", "قارن", "شراء", "أشتري", "اشتري",
    )
    return any(marker in text for marker in markers)


def extract_topic(message: str, products: list) -> str:
    """Return a short conversational label without inventing a product identity."""
    text = " ".join(str(message or "").split())
    lowered = text.lower()
    for product in products or []:
        name = str(product.get("name") or "").strip()
        model = str(product.get("model_number") or "").strip()
        if name and name.lower() in lowered:
            return name[:120]
        if model and re.search(r"(?<!\w)" + re.escape(model.lower()) + r"(?!\w)", lowered):
            return f"الموديل {model}"[:120]
    if "هدية" in text or "هديه" in text:
        return "الهدية التي تبحث عنها"
    if "ميزانية" in text:
        return "الخيارات المناسبة لميزانيتك"
    return "المنتج الذي سألت عنه"


def format_loyalty_summary(points: int, lifetime_points: int, minimum: int, value_per_point: float) -> str:
    return (
        "رصيد الولاء في هذا المتجر فقط:\n"
        f"• النقاط المتاحة: {max(0, int(points))}\n"
        f"• إجمالي النقاط المكتسبة: {max(0, int(lifetime_points))}\n"
        f"• الحد الأدنى لاستبدال المكافآت: {max(1, int(minimum))} نقطة\n"
        f"• القيمة الحسابية للنقطة: {max(0.0, float(value_per_point)):g} ريال، "
        "ولا تُخصم النقاط إلا عند استبدال مكافأة صالحة."
    )
