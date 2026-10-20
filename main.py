
import os
import re
import asyncio
import hashlib
import hmac
import logging
import requests
import tempfile
import uuid
import threading

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

from bot_logic import generate_ai_response, transcribe_audio
from telegram_products import import_telegram_product_post

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_PRODUCTS_CHAT_ID = os.getenv("TELEGRAM_PRODUCTS_CHAT_ID", "").strip()
# Empty by default: never silently import a catalog into a hard-coded business.
TELEGRAM_PRODUCTS_BUSINESS_ID = os.getenv("TELEGRAM_PRODUCTS_BUSINESS_ID", "").strip()
TELEGRAM_PRODUCTS_CURRENCY = os.getenv("TELEGRAM_PRODUCTS_CURRENCY", "YER").strip().upper()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

supabase = None

if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    try:
        from supabase import create_client

        supabase = create_client(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
        )
        logger.info("Supabase client initialized.")
    except Exception:
        logger.exception("تعذر تهيئة اتصال Supabase.")
else:
    logger.warning("إعدادات Supabase غير مكتملة.")


# -----------------------------
# Business data
# -----------------------------

def get_business_context(business_id: str):
    if not supabase or not business_id:
        return None

    try:
        business_result = (
            supabase.table("businesses")
            .select("id, name, industry, description")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )

        if not business_result.data:
            return None

        business = business_result.data[0]
        settings = {}

        try:
            settings_result = (
                supabase.table("business_settings")
                .select(
                    "business_description, business_policies, "
                    "business_hours, currency"
                )
                .eq("business_id", business_id)
                .limit(1)
                .execute()
            )
            if settings_result.data:
                settings = settings_result.data[0]
        except Exception:
            logger.warning(
                "تعذر قراءة إعدادات النشاط؛ ستستخدم المعلومات المتاحة."
            )

        products_result = (
            supabase.table("products")
            .select(
                "name, description, price, currency, is_available"
            )
            .eq("business_id", business_id)
            .execute()
        )

        lines = [
            f"اسم النشاط: {business.get('name') or 'غير محدد'}",
            f"المجال: {business.get('industry') or 'غير محدد'}",
            (
                "الوصف: "
                f"{settings.get('business_description') or business.get('description') or ''}"
            ),
            (
                "ساعات العمل: "
                f"{settings.get('business_hours') or 'غير محددة'}"
            ),
            (
                "السياسات: "
                f"{settings.get('business_policies') or 'غير محددة'}"
            ),
            f"العملة الافتراضية: {settings.get('currency') or 'YER'}",
            "",
            "المنتجات:",
        ]

        products = products_result.data or []

        if not products:
            lines.append("لا توجد منتجات مسجلة.")
        else:
            for product in products:
                availability = (
                    "متاح"
                    if product.get("is_available")
                    else "غير متاح"
                )
                lines.append(
                    f"- {product.get('name') or 'منتج'}: "
                    f"{product.get('description') or ''}; "
                    f"السعر: {product.get('price')}; "
                    f"العملة: {product.get('currency') or settings.get('currency') or 'YER'}; "
                    f"الحالة: {availability}"
                )

        return "\n".join(lines)

    except Exception:
        logger.exception("تعذر قراءة بيانات النشاط التجاري.")
        return None


def get_channel_business_id(channel: str, external_account_id: str):
    """Look up a tenant mapping for a specific connected channel/account."""
    if not supabase or not channel or not external_account_id:
        return None
    try:
        result = (
            supabase.table("channel_connections")
            .select("business_id")
            .eq("channel", channel)
            .eq("external_account_id", str(external_account_id))
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0].get("business_id") or None
    except Exception:
        logger.exception("تعذر تحديد النشاط المرتبط بالقناة %s.", channel)
    return None


def get_telegram_business_id(chat_id: int):
    if not supabase:
        return None

    try:
        result = (
            supabase.table("conversations")
            .select("business_id")
            .eq("channel", "telegram")
            .eq("customer_external_id", str(chat_id))
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )

        if result.data:
            return result.data[0].get("business_id")

    except Exception:
        logger.exception("تعذر تحديد نشاط محادثة Telegram.")

    return None


def save_telegram_business(chat_id: int, business_id: str):
    if not supabase:
        return False

    try:
        business_result = (
            supabase.table("businesses")
            .select("id")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )

        if not business_result.data:
            return False

        supabase.table("conversations").insert({
            "business_id": business_id,
            "channel": "telegram",
            "customer_external_id": str(chat_id),
            "status": "open",
        }).execute()

        return True

    except Exception:
        logger.exception("تعذر حفظ ارتباط Telegram بالنشاط.")
        return False


def get_available_products(business_id: str):
    if not supabase or not business_id:
        return []

    try:
        result = (
            supabase.table("products")
            .select("name, description, price, currency, is_available")
            .eq("business_id", business_id)
            .eq("is_available", True)
            .execute()
        )
        return result.data or []

    except Exception:
        logger.exception("تعذر قراءة المنتجات المتاحة.")
        return []


# -----------------------------
# Telegram order management
# -----------------------------

# تُحفظ الطلبات الجارية في Supabase حتى تبقى بعد إعادة تشغيل Render.
# تبقى الذاكرة المحلية كخطة احتياطية إذا لم يُنشأ الجدول بعد.
pending_telegram_orders = {}
order_lock = threading.Lock()


def load_pending_telegram_order(chat_id: int):
    if supabase is not None:
        try:
            result = (
                supabase.table("telegram_pending_orders")
                .select("state")
                .eq("chat_id", int(chat_id))
                .limit(1)
                .execute()
            )
            if result.data:
                state = result.data[0].get("state")
                if isinstance(state, dict):
                    with order_lock:
                        pending_telegram_orders[chat_id] = state
                    return state
            with order_lock:
                pending_telegram_orders.pop(chat_id, None)
            return None
        except Exception:
            logger.warning(
                "تعذر تحميل حالة الطلب من Supabase؛ ستُستخدم الذاكرة المحلية. "
                "تأكد من إنشاء جدول telegram_pending_orders."
            )

    with order_lock:
        state = pending_telegram_orders.get(chat_id)
        return dict(state) if isinstance(state, dict) else None


def persist_pending_telegram_order(chat_id: int, state: dict):
    with order_lock:
        pending_telegram_orders[chat_id] = dict(state)

    if supabase is None:
        return False

    try:
        supabase.table("telegram_pending_orders").upsert(
            {
                "chat_id": int(chat_id),
                "business_id": state.get("business_id"),
                "state": state,
            },
            on_conflict="chat_id",
        ).execute()
        return True
    except Exception:
        logger.warning(
            "تعذر حفظ حالة الطلب في Supabase. أنشئ جدول "
            "telegram_pending_orders باستخدام ملف SQL المرفق."
        )
        return False


def clear_pending_telegram_order(chat_id: int):
    with order_lock:
        pending_telegram_orders.pop(chat_id, None)

    if supabase is None:
        return

    try:
        supabase.table("telegram_pending_orders").delete().eq(
            "chat_id", int(chat_id)
        ).execute()
    except Exception:
        logger.warning(
            "تعذر حذف حالة الطلب من Supabase؛ تحقق من جدول "
            "telegram_pending_orders."
        )


def normalize_text(value: str):
    return " ".join(str(value or "").strip().lower().split())


def find_product(products, user_text):
    text = normalize_text(user_text)

    if not text:
        return None

    for product in products:
        name = normalize_text(product.get("name", ""))

        if name and (text == name or name in text):
            return product

    return None


def parse_quantity(value: str):
    digits = str(value or "").translate(
        str.maketrans(
            "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
            "01234567890123456789",
        )
    )

    match = re.search(r"\d+", digits)

    if not match:
        return None

    quantity = int(match.group())

    if quantity < 1 or quantity > 1000:
        return None

    return quantity


def save_telegram_order(chat_id: int, order_data: dict):
    """حفظ طلب مؤكد مع رقم محادثة تيليجرام."""

    if not supabase:
        logger.error("تعذر تسجيل الطلب: Supabase غير متصل.")
        return None

    business_id = order_data.get("business_id")

    if not business_id:
        logger.error("تعذر تسجيل الطلب: business_id غير موجود.")
        return None

    required_fields = (
        "customer_name",
        "customer_phone",
        "customer_address",
        "payment_method",
        "product_name",
    )

    if any(
        not str(order_data.get(field, "")).strip()
        for field in required_fields
    ):
        return None

    quantity = parse_quantity(str(order_data.get("quantity", 1)))

    if not quantity:
        return None

    try:
        business_result = (
            supabase.table("businesses")
            .select("id")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )

        if not business_result.data:
            logger.error("النشاط التجاري المرتبط بالطلب غير موجود.")
            return None

        product_result = (
            supabase.table("products")
            .select("name, is_available, price, currency")
            .eq("business_id", business_id)
            .eq("name", order_data["product_name"])
            .eq("is_available", True)
            .limit(1)
            .execute()
        )

        if not product_result.data:
            logger.warning("المنتج لم يعد متاحًا؛ لم يُحفظ الطلب.")
            return None

        product = product_result.data[0]
        base_order = {
            "business_id": business_id,
            "customer_name": order_data["customer_name"].strip(),
            "customer_phone": order_data["customer_phone"].strip(),
            "customer_address": order_data["customer_address"].strip(),
            "payment_method": order_data["payment_method"].strip(),
            "product_name": product["name"],
            "quantity": quantity,
            "status": "pending",
            "telegram_chat_id": str(chat_id),
        }

        # Keep the price at purchase time for accurate historical sales.
        # The fallback keeps existing databases working until migration is run.
        try:
            unit_price = float(product.get("price") or 0)
            total_price = round(unit_price * quantity, 2)
            result = (
                supabase.table("orders")
                .insert({
                    **base_order,
                    "unit_price": unit_price,
                    "total_price": total_price,
                    "currency": product.get("currency") or "YER",
                })
                .select("id, order_number, status")
                .execute()
            )
        except Exception as snapshot_error:
            error_text = str(snapshot_error).lower()
            missing_snapshot_column = any(
                column in error_text
                for column in ("unit_price", "total_price", "currency")
            ) and any(
                marker in error_text
                for marker in ("column", "schema cache", "does not exist", "could not find")
            )
            if not missing_snapshot_column:
                raise
            logger.warning(
                "Order price snapshot columns are unavailable; retrying "
                "with legacy fields. Apply the SQL migration for accurate reports."
            )
            result = (
                supabase.table("orders")
                .insert(base_order)
                .select("id, order_number, status")
                .execute()
            )

        if not result.data:
            logger.error("لم تُرجع قاعدة البيانات سجل الطلب.")
            return None

        return result.data[0]

    except Exception:
        logger.exception("فشل حفظ طلب Telegram في Supabase.")
        return None


def begin_telegram_order(chat_id: int, business_id: str, user_text: str):
    products = get_available_products(business_id)

    if not products:
        return "عذرًا، لا توجد منتجات متاحة للطلب حاليًا."

    selected = find_product(products, user_text)

    state = {
        "business_id": business_id,
        "stage": "quantity" if selected else "product",
        "product_name": selected.get("name") if selected else None,
        "quantity": 1,
    }
    persist_pending_telegram_order(chat_id, state)

    if selected:
        return (
            f"تم اختيار: {selected['name']}\n"
            "كم قطعة تريد طلبها؟ أرسل العدد بالأرقام."
        )

    product_lines = [
        f"- {product.get('name')}: {product.get('price')} "
        f"{product.get('currency') or 'YER'}"
        for product in products
    ]

    return (
        "يسعدني مساعدتك في تسجيل طلبك.\n"
        "اختر المنتج الذي تريد طلبه من المنتجات المتاحة:\n\n"
        + "\n".join(product_lines)
        + "\n\nأرسل اسم المنتج كما يظهر في القائمة، أو أرسل /cancel للإلغاء."
    )


def handle_telegram_order(chat_id: int, user_text: str, business_id: str):
    text = user_text.strip()
    normalized = normalize_text(text)

    if normalized in (
        "/cancel",
        "إلغاء",
        "الغاء",
        "الغاء الطلب",
        "إلغاء الطلب",
    ):
        clear_pending_telegram_order(chat_id)
        return "تم إلغاء الطلب الجاري. يمكنك البدء من جديد متى شئت."

    state = load_pending_telegram_order(chat_id)

    # Do not mistake a product question for an answer to an unfinished order
    # (for example, treating "أريد معرفة سعر السماعة" as the customer's name).
    product_question_markers = (
        "سعر", "بكم", "كم السعر", "معرفة سعر", "اعرف سعر",
        "تفاصيل المنتج", "معلومات المنتج", "مواصفات", "هل متوفر",
        "هل متاحة", "متوفر", "متاحة", "عندكم", "عندكم منه",
        "لون المنتج", "ما لون", "صور المنتج",
    )
    if state is not None and any(marker in normalized for marker in product_question_markers):
        return None

    order_intents = (
        "أريد طلب",
        "اريد طلب",
        "أريد شراء",
        "اريد شراء",
        "أبغى أطلب",
        "ابغى اطلب",
        "طلب منتج",
        "تسجيل طلب",
        "أريد أن أطلب",
        "اريد ان اطلب",
    )

    if state is None:
        if not any(intent in text for intent in order_intents):
            return None

        if not business_id:
            return (
                "لا يمكنني تسجيل الطلب بعد لأن هذه المحادثة غير مرتبطة "
                "بنشاط تجاري. افتح رابط البوت الخاص بالنشاط ثم حاول مجددًا."
            )

        return begin_telegram_order(chat_id, business_id, text)

    if not state.get("business_id"):
        clear_pending_telegram_order(chat_id)
        return "تعذر تحديد النشاط التجاري. ابدأ الطلب من رابط النشاط الصحيح."

    stage = state.get("stage")

    if stage == "product":
        products = get_available_products(state["business_id"])
        selected = find_product(products, text)

        if not selected:
            names = "\n".join(
                f"- {product.get('name')}"
                for product in products
            )
            return (
                "لم أجد هذا المنتج ضمن المنتجات المتاحة. "
                "اختر اسمًا من القائمة التالية:\n"
                f"{names}\n\nأرسل /cancel للإلغاء."
            )

        with order_lock:
            state["product_name"] = selected["name"]
            state["stage"] = "quantity"
        persist_pending_telegram_order(chat_id, state)

        return (
            f"تم اختيار: {selected['name']}\n"
            "كم قطعة تريد طلبها؟ أرسل العدد بالأرقام."
        )

    if stage == "quantity":
        quantity = parse_quantity(text)

        if not quantity:
            return "أرسل كمية صحيحة من 1 إلى 1000."

        with order_lock:
            state["quantity"] = quantity
            state["stage"] = "customer_name"
        persist_pending_telegram_order(chat_id, state)

        return "ما اسمك الكامل؟"

    if stage == "customer_name":
        if len(text) < 2 or len(text) > 150:
            return "يرجى إدخال الاسم بصورة صحيحة."

        with order_lock:
            state["customer_name"] = text
            state["stage"] = "customer_phone"
        persist_pending_telegram_order(chat_id, state)

        return "ما رقم الهاتف الذي يمكن التواصل معك من خلاله؟"

    if stage == "customer_phone":
        phone = re.sub(r"[^\d+]", "", text)

        if not re.fullmatch(r"\+?\d{7,15}", phone):
            return (
                "يرجى إدخال رقم هاتف صحيح يتكون من 7 إلى 15 رقمًا، "
                "ويمكنك إضافة علامة + في البداية."
            )

        with order_lock:
            state["customer_phone"] = phone
            state["stage"] = "customer_address"
        persist_pending_telegram_order(chat_id, state)

        return "ما عنوان التوصيل الكامل؟ اذكر المدينة والمنطقة والشارع."

    if stage == "customer_address":
        if len(text) < 5 or len(text) > 500:
            return "يرجى إدخال عنوان توصيل أوضح."

        with order_lock:
            state["customer_address"] = text
            state["stage"] = "payment_method"
        persist_pending_telegram_order(chat_id, state)

        return (
            "ما طريقة الدفع المفضلة لديك؟\n"
            "اذكر الطريقة المتاحة التي تفضلها، مثل المحفظة الإلكترونية "
            "أو التحويل البنكي. لن نؤكد توفر وسيلة دفع قبل التحقق منها."
        )

    if stage == "payment_method":
        if len(text) < 2 or len(text) > 100:
            return "يرجى إدخال طريقة الدفع التي تفضلها."

        with order_lock:
            state["payment_method"] = text
            state["stage"] = "confirmation"
        persist_pending_telegram_order(chat_id, state)

        return (
            "يرجى مراجعة تفاصيل طلبك:\n\n"
            f"المنتج: {state['product_name']}\n"
            f"الكمية: {state['quantity']}\n"
            f"الاسم: {state['customer_name']}\n"
            f"الهاتف: {state['customer_phone']}\n"
            f"العنوان: {state['customer_address']}\n"
            f"طريقة الدفع المطلوبة: {state['payment_method']}\n\n"
            "هل تؤكد رغبتك في تسجيل الطلب؟\n"
            "أرسل نعم للتأكيد أو لا للإلغاء."
        )

    if stage == "confirmation":
        yes_values = (
            "نعم",
            "أؤكد",
            "اكد",
            "تأكيد",
            "تأكيد الطلب",
            "yes",
            "confirm",
        )
        no_values = (
            "لا",
            "إلغاء",
            "الغاء",
            "لا أريد",
            "no",
            "cancel",
        )

        if normalized in yes_values:
            saved = save_telegram_order(chat_id, state)

            if not saved:
                return (
                    "لم أتمكن من تسجيل الطلب في قاعدة البيانات، "
                    "لذلك لم يتم تأكيده. يرجى المحاولة لاحقًا أو التواصل "
                    "مع خدمة العملاء."
                )

            clear_pending_telegram_order(chat_id)

            return (
                "تم تسجيل طلبك بنجاح في النظام. ✅\n\n"
                f"رقم الطلب: {saved.get('order_number')}\n"
                f"الحالة: {saved.get('status')}\n\n"
                "الحالة الحالية هي قيد المراجعة، وليست تأكيدًا للتوصيل "
                "أو استلام الدفع."
            )

        if normalized in no_values:
            with order_lock:
                pending_telegram_orders.pop(chat_id, None)
            return "تم إلغاء الطلب، ولم يتم حفظه."

        return "أرسل «نعم» لتأكيد الطلب، أو «لا» لإلغائه."

    clear_pending_telegram_order(chat_id)

    return "انتهت حالة الطلب. أرسل «أريد طلب» للبدء من جديد."


# -----------------------------
# WhatsApp business context
# -----------------------------

def get_whatsapp_business_id(phone_number_id: str):
    """Resolve the inbound WhatsApp business number to its tenant."""
    return get_channel_business_id("whatsapp", phone_number_id)


# -----------------------------
# WhatsApp functions
# -----------------------------

def send_whatsapp_message(to_phone: str, message_text: str, phone_number_id: str = None):
    active_phone_number_id = str(phone_number_id or PHONE_NUMBER_ID or "").strip()
    if not WHATSAPP_TOKEN or not active_phone_number_id:
        logger.error("إعدادات WhatsApp غير مكتملة أو رقم النشاط غير مربوط.")
        return

    url = (
        f"https://graph.facebook.com/v19.0/"
        f"{active_phone_number_id}/messages"
    )

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    data = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": message_text},
    }

    try:
        response = requests.post(
            url,
            json=data,
            headers=headers,
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException:
        logger.exception("فشل إرسال رسالة WhatsApp.")


def download_whatsapp_media(media_id: str):
    if not WHATSAPP_TOKEN or not media_id:
        return None

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}"
    }

    endpoint = f"https://graph.facebook.com/v19.0/{media_id}"

    try:
        response = requests.get(
            endpoint,
            headers=headers,
            timeout=20,
        )
        response.raise_for_status()

        media_url = response.json().get("url")

        if not media_url:
            return None

        media_response = requests.get(
            media_url,
            headers=headers,
            timeout=30,
        )
        media_response.raise_for_status()

        return media_response.content

    except requests.RequestException:
        logger.exception("فشل تحميل وسائط WhatsApp.")
        return None


# -----------------------------
# Telegram functions
# -----------------------------

def telegram_api(method: str, payload=None, timeout=20):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN غير موجود في إعدادات Render."
        )

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/{method}"
    )

    response = requests.post(
        url,
        json=payload or {},
        timeout=timeout,
    )

    try:
        result = response.json()
    except ValueError:
        result = {}

    if not response.ok or not result.get("ok"):
        description = result.get(
            "description",
            response.text[:1000] or "Unknown Telegram error",
        )

        logger.error(
            "Telegram API error: method=%s, HTTP=%s, description=%s",
            method,
            response.status_code,
            description,
        )

        raise RuntimeError(
            f"Telegram API error for {method}: "
            f"HTTP {response.status_code}: {description}"
        )

    return result.get("result")


async def send_telegram_message(chat_id: int, text: str):
    text = str(text or "عذرًا، لا يوجد رد حاليًا.")

    for start in range(0, len(text), 4000):
        await asyncio.to_thread(
            telegram_api,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text[start:start + 4000],
            },
            20,
        )


async def configure_telegram_webhook():
    """Configure Telegram webhook on Render to avoid competing getUpdates pollers."""
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("Telegram webhook skipped: token is not configured.")
        return

    base_url = os.getenv(
        "RENDER_EXTERNAL_URL",
        "https://whatsapp-a-avp6.onrender.com",
    ).rstrip("/")
    webhook_url = f"{base_url}/telegram/webhook"
    secret_token = hashlib.sha256(
        TELEGRAM_BOT_TOKEN.encode("utf-8")
    ).hexdigest()[:48]

    try:
        await asyncio.to_thread(
            telegram_api,
            "setWebhook",
            {
                "url": webhook_url,
                "secret_token": secret_token,
                "allowed_updates": ["message", "channel_post", "edited_channel_post"],
                "drop_pending_updates": False,
            },
            20,
        )
        logger.info("Telegram webhook configured at %s", webhook_url)
    except Exception:
        # Keep the API online while logging setup errors for diagnosis.
        logger.exception("Failed to configure Telegram webhook.")


# -----------------------------
# FastAPI lifecycle
# -----------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Use webhook delivery instead of polling to prevent Telegram 409 conflicts.
    await configure_telegram_webhook()
    try:
        yield
    finally:
        logger.info("Application shutdown complete.")


app = FastAPI(lifespan=lifespan)

# ربط لوحة التحكم الإدارية
from admin_dashboard import create_admin_router

if supabase is not None:
    app.include_router(create_admin_router(supabase))
else:
    logger.error("لوحة التحكم لم تُربط لأن Supabase غير متصل.")


# -----------------------------
# Telegram webhook
# -----------------------------

# Telegram may redeliver an update when webhook processing is slow.
# Keep a bounded in-process cache to avoid replying twice to recent duplicates.
telegram_update_lock = threading.Lock()
telegram_updates_in_progress = set()
telegram_updates_processed = set()
telegram_updates_processed_order = []
TELEGRAM_UPDATE_CACHE_LIMIT = 2000


def claim_telegram_update(update_id):
    if update_id is None:
        return True

    key = str(update_id)
    with telegram_update_lock:
        if key in telegram_updates_processed or key in telegram_updates_in_progress:
            return False
        telegram_updates_in_progress.add(key)
        return True


def finish_telegram_update(update_id):
    if update_id is None:
        return

    key = str(update_id)
    with telegram_update_lock:
        telegram_updates_in_progress.discard(key)
        if key in telegram_updates_processed:
            return
        telegram_updates_processed.add(key)
        telegram_updates_processed_order.append(key)

        while len(telegram_updates_processed_order) > TELEGRAM_UPDATE_CACHE_LIMIT:
            oldest = telegram_updates_processed_order.pop(0)
            telegram_updates_processed.discard(oldest)


def download_telegram_photo(file_id: str):
    """Download and validate a Telegram photo, with a 10 MB processing limit."""
    if not TELEGRAM_BOT_TOKEN or not file_id:
        logger.warning("Telegram photo download skipped: token or file_id is missing.")
        return None

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    try:
        metadata_response = requests.get(
            f"{api_url}/getFile",
            params={"file_id": file_id},
            timeout=20,
        )
        metadata_response.raise_for_status()
        metadata = metadata_response.json()

        if not metadata.get("ok"):
            logger.warning(
                "Telegram getFile failed: %s",
                metadata.get("description", "unknown Telegram API error"),
            )
            return None

        file_path = (metadata.get("result") or {}).get("file_path")
        if not file_path:
            logger.warning("Telegram getFile response did not include file_path.")
            return None

        file_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
        image_data = bytearray()
        max_bytes = 10 * 1024 * 1024

        with requests.get(file_url, timeout=30, stream=True) as image_response:
            image_response.raise_for_status()
            # Do not reject solely by Content-Type: Telegram/CDN may return
            # application/octet-stream even when the bytes are a valid photo.
            for chunk in image_response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                image_data.extend(chunk)
                if len(image_data) > max_bytes:
                    logger.warning("Telegram photo exceeded the 10 MB processing limit.")
                    return None

        data = bytes(image_data)
        is_jpeg = data.startswith(b"\xff\xd8\xff")
        is_png = data.startswith(b"\x89PNG\r\n\x1a\n")
        is_webp = len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"

        if not data or not (is_jpeg or is_png or is_webp):
            logger.warning(
                "Telegram file was downloaded but does not look like a supported image "
                "(bytes=%s, content_type=%s).",
                len(data),
                image_response.headers.get("Content-Type", "unknown"),
            )
            return None

        # Telegram's photo[] entries are normally JPEGs. Convert other accepted
        # image types to JPEG before sending to the model, if Pillow is available.
        if not is_jpeg:
            try:
                from io import BytesIO
                from PIL import Image

                with Image.open(BytesIO(data)) as image:
                    output = BytesIO()
                    image.convert("RGB").save(output, format="JPEG", quality=90)
                    data = output.getvalue()
            except Exception:
                logger.exception("Could not normalize Telegram photo to JPEG.")
                return None

        logger.info("Telegram photo downloaded successfully (%s bytes).", len(data))
        return data

    except requests.RequestException:
        logger.exception("Telegram photo HTTP request failed.")
        return None
    except (ValueError, TypeError):
        logger.exception("Invalid Telegram file metadata.")
        return None


async def respond_to_telegram_text(chat_id: int, user_text: str):
    """Route recognized voice text through the same order and AI flow as typed text."""
    try:
        business_id = await asyncio.to_thread(
            get_telegram_business_id, chat_id
        )
        if not business_id:
            return (
                "هذه المحادثة غير مرتبطة بمتجر بعد. افتح رابط البوت الخاص بالمتجر "
                "واضغط Start أولًا حتى أجيب من كتالوجه الصحيح."
            )
        order_reply = await asyncio.to_thread(
            handle_telegram_order, chat_id, user_text, business_id
        )

        if order_reply is not None:
            return order_reply
        if user_text.startswith("/"):
            return "الأوامر المتاحة: /start و /cancel"

        context = (
            await asyncio.to_thread(get_business_context, business_id)
            if business_id else None
        )
        return await asyncio.to_thread(
            generate_ai_response,
            f"telegram_{chat_id}",
            user_text,
            None,
            context,
            f"telegram_{chat_id}_{business_id or 'default'}",
        )
    except Exception:
        logger.exception("فشل معالجة نص Telegram (نص مكتوب أو مستخرج من الصوت).")
        return "عذرًا، حدث خطأ مؤقت أثناء معالجة رسالتك. يرجى المحاولة مرة أخرى."


def download_telegram_audio(file_id: str):
    """Download a Telegram voice/audio attachment, limited to 20 MB."""
    if not TELEGRAM_BOT_TOKEN or not file_id:
        logger.warning("Telegram audio download skipped: token or file_id is missing.")
        return None

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    try:
        metadata_response = requests.get(
            f"{api_url}/getFile",
            params={"file_id": file_id},
            timeout=20,
        )
        metadata_response.raise_for_status()
        metadata = metadata_response.json()
        if not metadata.get("ok"):
            logger.warning(
                "Telegram getFile for audio failed: %s",
                metadata.get("description", "unknown Telegram API error"),
            )
            return None

        file_path = (metadata.get("result") or {}).get("file_path")
        if not file_path:
            logger.warning("Telegram audio metadata did not include file_path.")
            return None

        suffix = os.path.splitext(file_path)[1].lower()
        if suffix == ".oga":
            # Telegram voice notes use Ogg/Opus; use the commonly accepted .ogg extension.
            suffix = ".ogg"
        if suffix not in {".ogg", ".mp3", ".m4a", ".wav", ".webm", ".mp4", ".mpeg", ".mpga", ".flac"}:
            suffix = ".ogg"
        file_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
        data = bytearray()
        max_bytes = 20 * 1024 * 1024

        with requests.get(file_url, timeout=40, stream=True) as audio_response:
            audio_response.raise_for_status()
            for chunk in audio_response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                data.extend(chunk)
                if len(data) > max_bytes:
                    logger.warning("Telegram audio exceeded the 20 MB download limit.")
                    return None

        if not data:
            logger.warning("Telegram audio download returned an empty file.")
            return None

        logger.info("Telegram audio downloaded successfully (%s bytes).", len(data))
        return bytes(data), suffix

    except requests.RequestException:
        logger.exception("Telegram audio HTTP request failed.")
        return None
    except (ValueError, TypeError):
        logger.exception("Invalid Telegram audio metadata.")
        return None


async def process_telegram_update(update: dict):
    # Product publishing channels are import-only: never reply to their posts.
    # Customer support is restricted to private chats to avoid group spam.
    # Import product posts from the configured Telegram source channel.
    source_post = update.get("channel_post") or update.get("edited_channel_post")
    if source_post:
        source_chat = source_post.get("chat") or {}
        source_chat_id = str(source_chat.get("id", ""))
        configured_chat_id = TELEGRAM_PRODUCTS_CHAT_ID
        configured_username = (
            configured_chat_id.lstrip("@").casefold()
            if configured_chat_id and not configured_chat_id.lstrip("-").isdigit()
            else ""
        )
        source_username = str(source_chat.get("username") or "").casefold()
        matches_source = (
            configured_chat_id == source_chat_id
            or (configured_username and source_username == configured_username)
        )
        if configured_chat_id and matches_source:
            try:
                # Prefer an explicit per-channel tenant mapping. The environment
                # fallback is only honored when the owner deliberately sets it.
                business_id = await asyncio.to_thread(
                    get_channel_business_id, "telegram_products", source_chat_id
                )
                if not business_id:
                    business_id = TELEGRAM_PRODUCTS_BUSINESS_ID or None
                if not business_id:
                    logger.error(
                        "Telegram product import skipped: map this source channel "
                        "to a business in channel_connections or explicitly set "
                        "TELEGRAM_PRODUCTS_BUSINESS_ID."
                    )
                    return
                result = await asyncio.to_thread(
                    import_telegram_product_post,
                    source_post,
                    supabase,
                    business_id,
                    TELEGRAM_PRODUCTS_CURRENCY,
                )
                logger.info(
                    "Telegram product post processed: chat_id=%s result=%s",
                    source_chat_id,
                    result,
                )
            except Exception:
                logger.exception("Failed to import a product from Telegram.")
        return

    message = update.get("message") or {}
    chat = message.get("chat") or {}
    # Customer service should answer in private chats only. Group messages
    # are ignored, while channel_post updates are handled by the import branch.
    if chat.get("type") != "private":
        return
    chat_id = chat.get("id")
    user_text = (message.get("text") or "").strip()
    caption = (message.get("caption") or "").strip()
    photos = message.get("photo") or []
    audio_attachment = message.get("voice") or message.get("audio")
    command = user_text.split(maxsplit=1)[0].lower() if user_text else ""

    if not chat_id or (not user_text and not photos and not audio_attachment):
        return

    if user_text.startswith("/start"):
        parts = user_text.split(maxsplit=1)
        payload = parts[1].strip() if len(parts) > 1 else ""
        business_id = None

        if payload:
            try:
                business_id = str(uuid.UUID(payload))
            except ValueError:
                business_id = None

        linked = False
        if business_id:
            linked = await asyncio.to_thread(
                save_telegram_business, chat_id, business_id
            )

        if linked:
            reply = (
                "مرحبًا بك! 👋\n"
                "تم ربط هذه المحادثة بالنشاط التجاري.\n"
                "اكتب /products لعرض المنتجات والأسعار.\n"
                "اكتب /order لبدء طلب جديد، أو /cancel لإلغاء الطلب الجاري.\n"
                "يمكنك أيضًا إرسال سؤالك أو صورة أو رسالة صوتية."
            )
        else:
            reply = (
                "مرحبًا بك! 👋\n"
                "أنا مساعد خدمة العملاء الذكي.\n"
                "افتح رابط البوت الخاص بالمتجر المنشور في قناة المنتجات لربط المحادثة.\n"
                "بعد الربط استخدم /products لعرض المنتجات و /order لبدء طلب.\n"
                "يمكنك أيضًا إرسال سؤالك أو صورة أو رسالة صوتية."
            )

    elif command == "/help":
        reply = (
            "مساعدة سريعة 🤝\n"
            "/products — عرض المنتجات المتاحة وأسعارها\n"
            "/order — بدء طلب جديد\n"
            "/cancel — إلغاء الطلب الجاري\n"
            "يمكنك إرسال سؤالك كتابةً أو بصورة أو برسالة صوتية."
        )

    elif command == "/products":
        business_id = await asyncio.to_thread(
            get_telegram_business_id, chat_id
        )
        products = (
            await asyncio.to_thread(get_available_products, business_id)
            if business_id else []
        )
        if not business_id:
            reply = (
                "لم يتم ربط هذه المحادثة بمتجر بعد. "
                "افتح رابط البوت الخاص بالمتجر ثم اضغط Start."
            )
        elif not products:
            reply = "لا توجد منتجات متاحة حاليًا."
        else:
            lines = ["المنتجات المتاحة حاليًا:"]
            for product in products[:50]:
                name = product.get("name") or "منتج"
                price = product.get("price")
                currency = product.get("currency") or "YER"
                description = (product.get("description") or "").strip()
                line = f"• {name} — {price} {currency}"
                if description:
                    line += f"\n  {description[:180]}"
                lines.append(line)
            lines.append("\nلطلب منتج، اكتب /order ثم اتبع التعليمات.")
            reply = "\n".join(lines)

    elif command == "/order":
        business_id = await asyncio.to_thread(
            get_telegram_business_id, chat_id
        )
        if not business_id:
            reply = (
                "لم يتم ربط هذه المحادثة بمتجر بعد. "
                "افتح رابط البوت الخاص بالمتجر ثم اضغط Start."
            )
        else:
            reply = await asyncio.to_thread(
                begin_telegram_order, chat_id, business_id, ""
            )

    elif user_text.startswith("/cancel"):
        clear_pending_telegram_order(chat_id)
        reply = "تم إلغاء الطلب الجاري."

    elif photos:
        business_id = await asyncio.to_thread(
            get_telegram_business_id, chat_id
        )
        if not business_id:
            reply = (
                "هذه المحادثة غير مرتبطة بمتجر بعد. افتح رابط البوت الخاص "
                "بالمتجر واضغط Start أولًا حتى أساعدك من كتالوجه الصحيح."
            )
        else:
            # Telegram includes multiple sizes; the last item is usually the largest.
            photo = photos[-1]
            image_bytes = await asyncio.to_thread(
                download_telegram_photo,
                photo.get("file_id"),
            )

            if not image_bytes:
                reply = (
                    "عذرًا، لم أتمكن من تحميل الصورة أو أن حجمها أكبر من الحد المسموح. "
                    "أرسل صورة أصغر من 10 ميجابايت وحاول مجددًا."
                )
            else:
                try:
                    context = await asyncio.to_thread(get_business_context, business_id)
                    prompt = caption or (
                        "حلّل الصورة المرسلة من العميل، واشرح ما يظهر فيها "
                        "وساعده اعتمادًا على معلومات النشاط التجاري."
                    )
                    reply = await asyncio.to_thread(
                        generate_ai_response,
                        f"telegram_{chat_id}",
                        prompt,
                        image_bytes,
                        context,
                        f"telegram_{chat_id}_{business_id}",
                    )
                except Exception:
                    logger.exception("Failed to process Telegram photo with AI.")
                    reply = (
                        "وصلتني الصورة، لكن تعذّر تحليلها الآن. "
                        "يرجى المحاولة مرة أخرى أو كتابة سؤالك نصيًا."
                    )

    elif audio_attachment:
        audio_result = await asyncio.to_thread(
            download_telegram_audio,
            audio_attachment.get("file_id"),
        )

        if not audio_result:
            reply = (
                "عذرًا، لم أتمكن من تحميل الرسالة الصوتية. "
                "جرّب إرسال تسجيل أقصر، أو اكتب رسالتك نصيًا."
            )
        else:
            audio_bytes, audio_suffix = audio_result
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    delete=False,
                    suffix=audio_suffix,
                ) as temp_audio:
                    temp_audio.write(audio_bytes)
                    temp_path = temp_audio.name

                transcript = await asyncio.to_thread(
                    transcribe_audio,
                    temp_path,
                )
                logger.info(
                    "Telegram voice transcribed successfully (chat_id=%s, chars=%s).",
                    chat_id,
                    len(transcript),
                )
                recognized_text = transcript
                if caption:
                    recognized_text = f"{caption}\n{transcript}"
                reply = await respond_to_telegram_text(chat_id, recognized_text)
            except Exception as exc:
                logger.exception("Failed to transcribe/process Telegram voice message.")
                if "25 ميجابايت" in str(exc):
                    reply = "حجم الرسالة الصوتية كبير جدًا. أرسل تسجيلًا أقصر من فضلك."
                else:
                    reply = (
                        "وصلتني رسالتك الصوتية، لكن تعذّر تحويلها إلى نص الآن. "
                        "حاول مرة أخرى أو اكتب رسالتك."
                    )
            finally:
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        logger.warning("Could not remove temporary Telegram audio file.")

    else:
        reply = await respond_to_telegram_text(chat_id, user_text)

    await send_telegram_message(chat_id, reply)


@app.post("/telegram/webhook")
async def receive_telegram_webhook(request: Request):
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=503, detail="Telegram is not configured.")

    expected_secret = hashlib.sha256(
        TELEGRAM_BOT_TOKEN.encode("utf-8")
    ).hexdigest()[:48]
    supplied_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token", ""
    )
    if not hmac.compare_digest(supplied_secret, expected_secret):
        raise HTTPException(status_code=403, detail="Invalid webhook secret.")

    try:
        update = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    update_id = update.get("update_id")
    if not claim_telegram_update(update_id):
        logger.info("Ignoring duplicate Telegram update (update_id=%s).", update_id)
        return {"ok": True, "duplicate": True}

    try:
        await process_telegram_update(update)
    except Exception:
        logger.exception("Failed to process Telegram webhook update.")
    finally:
        finish_telegram_update(update_id)

    return {"ok": True}


# -----------------------------
# WhatsApp webhook
# -----------------------------

@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params

    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == VERIFY_TOKEN
    ):
        return Response(
            content=params.get("hub.challenge", ""),
            media_type="text/plain",
        )

    raise HTTPException(
        status_code=403,
        detail="Verification failed",
    )


@app.post("/webhook")
async def receive_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON body",
        )

    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                metadata = value.get("metadata") or {}
                inbound_phone_number_id = str(
                    metadata.get("phone_number_id") or PHONE_NUMBER_ID or ""
                ).strip()

                for message in value.get("messages", []):
                    from_number = message.get("from")
                    msg_type = message.get("type")

                    if not from_number:
                        continue

                    business_id = await asyncio.to_thread(
                        get_whatsapp_business_id, inbound_phone_number_id
                    )
                    if not business_id:
                        logger.warning(
                            "Ignoring WhatsApp message because phone number %s "
                            "is not mapped to a business.",
                            inbound_phone_number_id or "(missing)",
                        )
                        continue
                    context = await asyncio.to_thread(get_business_context, business_id)

                    user_text = ""
                    image_bytes = None

                    if msg_type == "text":
                        user_text = (
                            message.get("text", {}).get("body", "")
                        )

                    elif msg_type == "image":
                        image_data = message.get("image", {})

                        user_text = image_data.get(
                            "caption",
                            "يرجى وصف الصورة.",
                        )

                        image_bytes = await asyncio.to_thread(
                            download_whatsapp_media,
                            image_data.get("id"),
                        )

                        if not image_bytes:
                            await asyncio.to_thread(
                                send_whatsapp_message,
                                from_number,
                                "عذرًا، لم أتمكن من تحميل الصورة.",
                                inbound_phone_number_id,
                            )
                            continue

                    elif msg_type in ("audio", "voice"):
                        audio_data = message.get(msg_type, {})

                        audio_bytes = await asyncio.to_thread(
                            download_whatsapp_media,
                            audio_data.get("id"),
                        )

                        if not audio_bytes:
                            continue

                        temp_path = None

                        try:
                            with tempfile.NamedTemporaryFile(
                                delete=False,
                                suffix=".ogg",
                            ) as temp_audio:
                                temp_audio.write(audio_bytes)
                                temp_path = temp_audio.name

                            user_text = await asyncio.to_thread(
                                transcribe_audio,
                                temp_path,
                            )

                        except Exception:
                            logger.exception("فشل تحويل الرسالة الصوتية.")
                            continue

                        finally:
                            if temp_path and os.path.exists(temp_path):
                                os.remove(temp_path)

                    else:
                        continue

                    if not user_text and not image_bytes:
                        continue

                    ai_reply = await asyncio.to_thread(
                        generate_ai_response,
                        f"whatsapp_{from_number}",
                        user_text,
                        image_bytes,
                        context,
                        (
                            f"whatsapp_{PHONE_NUMBER_ID or 'default'}_"
                            f"{from_number}"
                        ),
                    )

                    await asyncio.to_thread(
                        send_whatsapp_message,
                        from_number,
                        ai_reply,
                        inbound_phone_number_id,
                    )

    except Exception:
        logger.exception("حدث خطأ أثناء معالجة Webhook واتساب.")

    return {"status": "ok"}


# -----------------------------
# Test AI endpoint
# -----------------------------

class TestAIRequest(BaseModel):
    message: str


@app.post("/test-ai")
async def test_ai(request: TestAIRequest):
    try:
        reply = await asyncio.to_thread(
            generate_ai_response,
            "test-session",
            request.message,
        )

        return {
            "success": True,
            "reply": reply,
        }

    except Exception:
        logger.exception("فشل اختبار الذكاء الاصطناعي.")

        raise HTTPException(
            status_code=500,
            detail=(
                "فشل اختبار الذكاء الاصطناعي. "
                "راجع سجلات Render."
            ),
        )


# -----------------------------
# Health check
# -----------------------------

@app.get("/")
async def home():
    return {
        "status": "running",
        "message": "Customer service bot is running.",
        "supabase_configured": supabase is not None,
    }
