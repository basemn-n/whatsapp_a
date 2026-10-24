
import os
import re
import json
import asyncio
import hashlib
import hmac
import logging
import requests
import tempfile
import uuid
import threading
import time
from datetime import datetime, timedelta, timezone
import httpx
from urllib.parse import urlparse

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from dotenv import load_dotenv

from bot_logic import generate_ai_response, generate_shopping_advice, find_products_by_image, transcribe_audio
from store_growth import extract_topic, format_loyalty_summary, is_product_inquiry, is_shopping_advice_request
from telegram_products import import_telegram_product_post

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
META_APP_SECRET = os.getenv("META_APP_SECRET", "").strip()
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
FACEBOOK_VERIFY_TOKEN = os.getenv("FACEBOOK_VERIFY_TOKEN", "").strip()
FACEBOOK_PAGE_ACCESS_TOKEN = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()
FACEBOOK_PAGE_ID = os.getenv("FACEBOOK_PAGE_ID", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@") or "SmartCustomerHelp2026Bot"
# Telegram catalog imports must use an explicit channel_connections mapping.
# Optional chat ID restricts which groups/channels are considered; it never chooses a store.
TELEGRAM_PRODUCTS_CHAT_ID = os.getenv("TELEGRAM_PRODUCTS_CHAT_ID", "").strip()
TELEGRAM_PRODUCTS_CURRENCY = os.getenv("TELEGRAM_PRODUCTS_CURRENCY", "YER").strip().upper()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

supabase = None

if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    try:
        from supabase import ClientOptions, create_client

        class SupabaseReadRetryClient(httpx.Client):
            """Avoid flaky HTTP/2 reads; retry safe read requests only."""

            def send(self, request, **kwargs):
                attempts = 3 if request.method in {"GET", "HEAD"} else 1
                for attempt in range(attempts):
                    try:
                        return super().send(request, **kwargs)
                    except (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError):
                        if attempt + 1 >= attempts:
                            raise
                        time.sleep(0.5 * (attempt + 1))

        supabase_http_client = SupabaseReadRetryClient(
            http2=False,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )
        supabase = create_client(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
            options=ClientOptions(httpx_client=supabase_http_client),
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
                "name, description, price, currency, is_available, "
                "model_number, category, stock_quantity"
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
                stock_quantity = product.get("stock_quantity")
                # Availability must respect both the merchant toggle and real stock.
                in_stock = stock_quantity is None or int(stock_quantity) > 0
                availability = (
                    "متاح للطلب"
                    if product.get("is_available") and in_stock
                    else "غير متاح للطلب"
                )
                model_number = str(product.get("model_number") or "").strip()
                category = str(product.get("category") or "").strip()
                stock_text = (
                    f"الكمية في المخزون: {stock_quantity}"
                    if stock_quantity is not None
                    else "كمية المخزون: غير محددة"
                )
                lines.append(
                    f"- اسم المنتج: {product.get('name') or 'منتج'}; "
                    f"التصنيف: {category or 'غير محدد'}; "
                    f"رقم الموديل/SKU: {model_number or 'غير محدد'}; "
                    f"الوصف: {product.get('description') or ''}; "
                    f"السعر: {product.get('price')}; "
                    f"العملة: {product.get('currency') or settings.get('currency') or 'YER'}; "
                    f"{stock_text}; "
                    f"حالة الطلب في لوحة التاجر: {availability}"
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



def get_visual_search_products(business_id: str):
    """Return only available products from the explicitly bound Telegram store."""
    if not supabase or not business_id:
        return []
    try:
        result = (
            supabase.table("products")
            .select(
                "id, name, description, price, currency, is_available, "
                "model_number, category, stock_quantity, image_url"
            )
            .eq("business_id", business_id)
            .eq("is_available", True)
            .order("name")
            .limit(100)
            .execute()
        )
        products = result.data or []
        available = []
        for product in products:
            stock = product.get("stock_quantity")
            if stock is not None:
                try:
                    if int(stock) <= 0:
                        continue
                except (TypeError, ValueError):
                    continue
            available.append(product)
        return available
    except Exception:
        logger.exception("تعذر تحميل كتالوج المتجر للبحث البصري.")
        return []


def get_available_products(business_id: str):
    if not supabase or not business_id:
        return []

    try:
        result = (
            supabase.table("products")
            .select("id, name, description, price, currency, is_available, model_number, category, stock_quantity")
            .eq("business_id", business_id)
            .eq("is_available", True)
            .execute()
        )
        # Filter depleted products in Python because PostgREST null/zero stock
        # semantics should not accidentally make them orderable.
        products = result.data or []
        available = []
        for product in products:
            stock = product.get("stock_quantity")
            if stock is not None:
                try:
                    if int(stock) <= 0:
                        continue
                except (TypeError, ValueError):
                    continue
            available.append(product)
        return available

    except Exception:
        logger.exception("تعذر قراءة المنتجات المتاحة.")
        return []


# -----------------------------
# Store growth: opt-in follow-ups and loyalty
# -----------------------------

def get_store_growth_settings(business_id: str):
    defaults = {
        "shopping_advisor_enabled": True,
        "followups_enabled": True,
        "followup_delay_hours": 24,
        "loyalty_enabled": True,
        "points_per_1000": 1,
        "point_value_yer": 1,
        "minimum_redeem_points": 20,
        "coupon_expiry_days": 30,
    }
    if not supabase or not business_id:
        return defaults
    try:
        result = (
            supabase.table("store_growth_settings")
            .select("*")
            .eq("business_id", business_id)
            .limit(1)
            .execute()
        )
        if result.data:
            return {**defaults, **result.data[0]}
    except Exception:
        logger.warning("Store growth settings unavailable; migration may not be applied yet.")
    return defaults


def set_telegram_reminder_preference(business_id: str, chat_id: int, enabled: bool):
    if not supabase or not business_id:
        return False
    try:
        supabase.table("telegram_customer_preferences").upsert({
            "business_id": business_id,
            "telegram_chat_id": str(chat_id),
            "reminders_opt_in": bool(enabled),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="business_id,telegram_chat_id").execute()
        if not enabled:
            cancel_customer_followup(business_id, chat_id)
        else:
            # If consent is given after the inquiry, queue only the most recent
            # unsatisfied inquiry from the last day; never revive one followed by an order.
            recent = (
                supabase.table("telegram_customer_followups")
                .select("id,created_at")
                .eq("business_id", business_id)
                .eq("telegram_chat_id", str(chat_id))
                .eq("status", "cancelled")
                .gte("created_at", (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat())
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            if recent.data:
                inquiry = recent.data[0]
                orders = (
                    supabase.table("orders")
                    .select("id")
                    .eq("business_id", business_id)
                    .eq("telegram_chat_id", str(chat_id))
                    .gte("created_at", inquiry.get("created_at"))
                    .neq("status", "cancelled")
                    .limit(1)
                    .execute()
                )
                if not orders.data:
                    settings = get_store_growth_settings(business_id)
                    delay_hours = max(1, min(168, int(settings.get("followup_delay_hours") or 24)))
                    supabase.table("telegram_customer_followups").update({
                        "status": "pending",
                        "due_at": (datetime.now(timezone.utc) + timedelta(hours=delay_hours)).isoformat(),
                    }).eq("id", inquiry["id"]).eq("status", "cancelled").execute()
        return True
    except Exception:
        logger.exception("Could not save Telegram reminder preference.")
        return False


def customer_has_reminder_consent(business_id: str, chat_id: int):
    if not supabase or not business_id:
        return False
    try:
        result = (
            supabase.table("telegram_customer_preferences")
            .select("reminders_opt_in")
            .eq("business_id", business_id)
            .eq("telegram_chat_id", str(chat_id))
            .limit(1)
            .execute()
        )
        return bool(result.data and result.data[0].get("reminders_opt_in"))
    except Exception:
        return False


def cancel_customer_followup(business_id: str, chat_id: int):
    if not supabase or not business_id:
        return
    try:
        supabase.table("telegram_customer_followups").update({
            "status": "cancelled",
        }).eq("business_id", business_id).eq(
            "telegram_chat_id", str(chat_id)
        ).eq("status", "pending").execute()
    except Exception:
        logger.warning("Could not cancel Telegram customer follow-up.")


def schedule_customer_followup(business_id: str, chat_id: int, user_text: str):
    """Remember a short inquiry label; queue delivery only after explicit consent."""
    if not supabase or not business_id:
        return False
    settings = get_store_growth_settings(business_id)
    if not settings.get("followups_enabled", True):
        return False
    try:
        products = get_available_products(business_id)
        topic = extract_topic(user_text, products)
        now = datetime.now(timezone.utc)
        delay_hours = max(1, min(168, int(settings.get("followup_delay_hours") or 24)))
        opted_in = customer_has_reminder_consent(business_id, chat_id)
        due_at = now + timedelta(hours=delay_hours)
        supabase.table("telegram_customer_followups").upsert({
            "business_id": business_id,
            "telegram_chat_id": str(chat_id),
            "topic": topic,
            "status": "pending" if opted_in else "cancelled",
            "due_at": due_at.isoformat(),
            "created_at": now.isoformat(),
            "sent_at": None,
        }, on_conflict="business_id,telegram_chat_id").execute()
        return opted_in
    except Exception:
        logger.exception("Could not schedule Telegram customer follow-up.")
        return False


def get_customer_loyalty(business_id: str, chat_id: int):
    if not supabase or not business_id:
        return None
    try:
        result = (
            supabase.table("store_loyalty_accounts")
            .select("points_balance,lifetime_points")
            .eq("business_id", business_id)
            .eq("customer_external_id", str(chat_id))
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else {"points_balance": 0, "lifetime_points": 0}
    except Exception:
        logger.exception("Could not load store loyalty account.")
        return None


async def send_due_telegram_followups():
    """One-shot, consented reminders. Mark sent before network delivery to avoid duplicates."""
    if not supabase or not TELEGRAM_BOT_TOKEN:
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        result = (
            supabase.table("telegram_customer_followups")
            .select("id,business_id,telegram_chat_id,topic,created_at")
            .eq("status", "pending")
            .lte("due_at", now)
            .limit(25)
            .execute()
        )
        for item in result.data or []:
            business_id = item.get("business_id")
            chat_id = str(item.get("telegram_chat_id") or "")
            if not business_id or not chat_id:
                continue
            settings = get_store_growth_settings(business_id)
            if not settings.get("followups_enabled", True) or not customer_has_reminder_consent(business_id, int(chat_id)):
                cancel_customer_followup(business_id, int(chat_id))
                continue
            # If a real order was created after the inquiry, the reminder is no longer relevant.
            try:
                orders = (
                    supabase.table("orders")
                    .select("id")
                    .eq("business_id", business_id)
                    .eq("telegram_chat_id", chat_id)
                    .gte("created_at", item.get("created_at"))
                    .neq("status", "cancelled")
                    .limit(1)
                    .execute()
                )
                if orders.data:
                    cancel_customer_followup(business_id, int(chat_id))
                    continue
            except Exception:
                # Fail closed: don't send if order-state verification is unavailable.
                logger.warning("Skipping reminder because order-state verification failed.")
                continue
            claimed = (
                supabase.table("telegram_customer_followups")
                .update({"status": "sent", "sent_at": now})
                .eq("id", item["id"])
                .eq("status", "pending")
                .select("id")
                .execute()
            )
            if not claimed.data:
                continue
            topic = str(item.get("topic") or "المنتج الذي سألت عنه")[:120]
            message = (
                f"مرحبًا! تذكير واحد بناءً على موافقتك: سألتنا عن «{topic}». "
                "هل ترغب في معرفة تفاصيل إضافية أو المساعدة في اختيار المنتج المناسب؟\n\n"
                "إذا لم تعد ترغب في التذكيرات، أرسل /reminders_off."
            )
            try:
                await send_telegram_message(int(chat_id), message)
            except Exception:
                logger.exception("Could not deliver a scheduled Telegram follow-up.")
    except Exception:
        logger.exception("Failed to process scheduled Telegram follow-ups.")


async def telegram_followup_worker():
    while True:
        await asyncio.sleep(60)
        await send_due_telegram_followups()


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


def clear_pending_order_if_store_changed(chat_id: int, business_id: str):
    """Discard an unfinished order only after a customer successfully switches stores."""
    state = load_pending_telegram_order(chat_id)
    if not isinstance(state, dict):
        return False

    pending_business_id = str(state.get("business_id") or "").strip()
    requested_business_id = str(business_id or "").strip()
    if requested_business_id and pending_business_id == requested_business_id:
        return False

    clear_pending_telegram_order(chat_id)
    return True


def normalize_text(value: str):
    return " ".join(str(value or "").strip().lower().split())


def find_product(products, user_text):
    text = normalize_text(user_text)
    if not text:
        return None

    # Prefer the stable model number when product names are duplicated.
    for product in products:
        model_number = normalize_text(str(product.get("model_number") or ""))
        if model_number and model_number == text:
            return product

    matches = []
    for product in products:
        name = normalize_text(product.get("name", ""))
        if name and (text == name or name in text):
            matches.append(product)

    # Never silently choose the first product when names are ambiguous.
    return matches[0] if len(matches) == 1 else None


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
    """Atomically validate stock, reserve units, and save a confirmed order."""

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
        "product_id",
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

        # The database function locks the exact selected product row and is the
        # single source of truth for stock reservation and the saved price snapshot.
        # It writes unit_price, total_price, and currency in the same transaction
        # as the order, avoiding a stale read or a second, non-atomic price update.
        result = supabase.rpc(
            "create_telegram_order_with_stock",
            {
                "p_business_id": business_id,
                "p_customer_name": order_data["customer_name"].strip(),
                "p_customer_phone": order_data["customer_phone"].strip(),
                "p_customer_address": order_data["customer_address"].strip(),
                "p_payment_method": order_data["payment_method"].strip(),
                "p_product_id": order_data["product_id"],
                "p_quantity": quantity,
                "p_telegram_chat_id": str(chat_id),
            },
        ).execute()

        payload = result.data
        if isinstance(payload, list):
            payload = payload[0] if payload else None
        if not isinstance(payload, dict):
            logger.error("وظيفة تسجيل الطلب لم تُرجع نتيجة مفهومة.")
            return None

        if not payload.get("success"):
            reason = payload.get("reason")
            if reason == "insufficient_stock":
                return {
                    "error": "insufficient_stock",
                    "available_stock": payload.get("available_stock", 0),
                }
            if reason == "product_unavailable":
                return {"error": "product_unavailable"}
            logger.error("تعذر تسجيل الطلب عبر الوظيفة الذرية: %s", reason)
            return None

        saved_order = payload.get("order") or {}
        cancel_customer_followup(business_id, chat_id)

        return {
            "order_number": saved_order.get("order_number"),
            "status": saved_order.get("status", "pending"),
        }

    except Exception:
        logger.exception("فشل تسجيل الطلب مع التحقق الذري من المخزون.")
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
        "product_id": selected.get("id") if selected else None,
        "quantity": 1,
    }
    persist_pending_telegram_order(chat_id, state)

    if selected:
        return (
            f"تم اختيار: {selected['name']}\n"
            "كم قطعة تريد طلبها؟ أرسل العدد بالأرقام."
        )

    product_lines = [
        f"- {product.get('name')} "
        f"(رقم الموديل: {product.get('model_number') or 'غير محدد'}): "
        f"{product.get('price')} {product.get('currency') or 'YER'}"
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
                f"- {product.get('name')} (رقم الموديل: {product.get('model_number') or 'غير محدد'})"
                for product in products
            )
            return (
                "لم أجد المنتج أو أن الاسم يطابق أكثر من منتج. "
                "اختر اسمًا مع رقم الموديل من القائمة التالية، أو أرسل رقم الموديل:\n"
                f"{names}\n\nأرسل /cancel للإلغاء."
            )

        with order_lock:
            state["product_name"] = selected["name"]
            state["product_id"] = selected["id"]
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

    if stage == "quantity_adjust":
        quantity = parse_quantity(text)
        if not quantity:
            return "أرسل كمية صحيحة من 1 إلى 1000."

        with order_lock:
            state["quantity"] = quantity
            state["stage"] = "confirmation"
        persist_pending_telegram_order(chat_id, state)

        return (
            "تم تحديث الكمية المطلوبة إلى "
            f"{quantity} قطعة.\n\n"
            f"المنتج: {state.get('product_name')}\n"
            f"الاسم: {state.get('customer_name')}\n"
            f"الهاتف: {state.get('customer_phone')}\n"
            f"العنوان: {state.get('customer_address')}\n"
            f"طريقة الدفع المطلوبة: {state.get('payment_method')}\n\n"
            "هل تؤكد تسجيل الطلب بهذه الكمية؟ أرسل نعم للتأكيد أو لا للإلغاء."
        )

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

            if isinstance(saved, dict) and saved.get("error") == "insufficient_stock":
                available = max(0, int(saved.get("available_stock") or 0))
                with order_lock:
                    state["stage"] = "quantity_adjust"
                    state["quantity"] = available
                persist_pending_telegram_order(chat_id, state)
                return (
                    "عذرًا، الكمية المطلوبة أكبر من المخزون المتاح حاليًا.\n"
                    f"المتاح الآن: {available} قطعة.\n"
                    "أرسل الكمية الجديدة التي تريدها، أو أرسل /cancel للإلغاء."
                )

            if isinstance(saved, dict) and saved.get("error") == "product_unavailable":
                clear_pending_telegram_order(chat_id)
                return (
                    "عذرًا، أصبح هذا المنتج غير متاح للطلب. "
                    "لم يتم تسجيل الطلب. يمكنك اختيار منتج آخر."
                )

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
                "تم حجز الكمية المطلوبة من المخزون. الحالة الحالية هي قيد المراجعة، "
                "وليست تأكيدًا للتوصيل أو استلام الدفع."
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
        f"https://graph.facebook.com/v26.0/"
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

    endpoint = f"https://graph.facebook.com/v26.0/{media_id}"

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
    followup_task = asyncio.create_task(telegram_followup_worker())
    try:
        yield
    finally:
        followup_task.cancel()
        try:
            await followup_task
        except asyncio.CancelledError:
            pass
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


def abandon_telegram_update(update_id):
    """Release an in-process claim after a failed update so Telegram can retry."""
    if update_id is None:
        return
    with telegram_update_lock:
        telegram_updates_in_progress.discard(str(update_id))


def claim_telegram_update_durable(update_id):
    """Atomically claim a Telegram update in Supabase; None means fallback unavailable."""
    if supabase is None or update_id is None:
        return None
    try:
        result = supabase.rpc(
            "claim_telegram_update",
            {"p_update_id": int(update_id)},
        ).execute()
        data = result.data
        if isinstance(data, bool):
            return data
        if isinstance(data, list) and data and isinstance(data[0], bool):
            return data[0]
        logger.warning("Telegram durable deduplication returned an unexpected response.")
    except Exception:
        logger.exception(
            "Durable Telegram deduplication unavailable; using the in-process cache."
        )
    return None


def release_telegram_update_durable(update_id):
    """Remove a durable claim after processing fails, allowing webhook retry."""
    if supabase is None or update_id is None:
        return
    try:
        supabase.rpc(
            "release_telegram_update",
            {"p_update_id": int(update_id)},
        ).execute()
    except Exception:
        logger.exception("Could not release failed Telegram update claim.")


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


shopping_advisor_last_turn = {}


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
            shopping_advisor_last_turn.pop(f"{business_id}:{chat_id}", None)
            return order_reply
        if user_text.startswith("/"):
            return "الأوامر المتاحة: /start و /cancel"

        settings = await asyncio.to_thread(get_store_growth_settings, business_id)
        advisor_key = f"{business_id}:{chat_id}"
        last_advisor_turn = shopping_advisor_last_turn.get(advisor_key, 0)
        advisor_continuation = (
            last_advisor_turn and time.monotonic() - last_advisor_turn < 600
            and load_pending_telegram_order(chat_id) is None
        )
        use_advisor = (
            settings.get("shopping_advisor_enabled", True)
            and (is_shopping_advice_request(user_text) or advisor_continuation)
        )
        if use_advisor:
            products = await asyncio.to_thread(get_available_products, business_id)
            reply = await asyncio.to_thread(
                generate_shopping_advice,
                user_text,
                products,
                business_id,
                f"shopping_advisor_telegram_{chat_id}_{business_id}",
            )
            shopping_advisor_last_turn[advisor_key] = time.monotonic()
            if len(shopping_advisor_last_turn) > 2000:
                shopping_advisor_last_turn.pop(next(iter(shopping_advisor_last_turn)))
        else:
            shopping_advisor_last_turn.pop(advisor_key, None)
            context = (
                await asyncio.to_thread(get_business_context, business_id)
                if business_id else None
            )
            reply = await asyncio.to_thread(
                generate_ai_response,
                f"telegram_{chat_id}",
                user_text,
                None,
                context,
                f"telegram_{chat_id}_{business_id or 'default'}",
                business_id,
            )

        if is_product_inquiry(user_text):
            await asyncio.to_thread(schedule_customer_followup, business_id, chat_id, user_text)
        return reply
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


def is_telegram_group_admin(chat_id: str, user_id):
    """Return True/False for a verified group admin; None when status cannot be checked."""
    if not chat_id or user_id is None:
        return None
    try:
        member = telegram_api(
            "getChatMember",
            {"chat_id": chat_id, "user_id": int(user_id)},
            15,
        )
        status = str((member or {}).get("status") or "").lower()
        return status in ("creator", "administrator")
    except Exception:
        logger.warning(
            "Could not verify Telegram group sender permissions; ignoring message safely."
        )
        return None


def telegram_chat_matches_configured(chat: dict, configured_chat_id: str) -> bool:
    """Match a Telegram chat allow-list by numeric ID or public @username."""
    configured = str(configured_chat_id or "").strip()
    if not configured:
        return True

    chat_id = str((chat or {}).get("id") or "").strip()
    if configured == chat_id:
        return True

    configured_username = configured.lstrip("@").casefold()
    chat_username = str((chat or {}).get("username") or "").strip().lstrip("@").casefold()
    # A username is a valid selector only when the incoming chat exposes one.
    return bool(
        configured_username
        and not configured_username.lstrip("-").isdigit()
        and chat_username
        and configured_username == chat_username
    )


def is_customer_question_or_request(text: str):
    """Avoid replying to greetings and ordinary group chatter."""
    value = normalize_text(text)
    if not value:
        return False
    markers = (
        "؟", "?", "اريد", "أريد", "ابغى", "أبغى", "بكم", "كم سعر",
        "السعر", "متوفر", "متوفره", "متوفرة", "عندكم", "هل يوجد",
        "كيف اطلب", "كيف أطلب", "طلب", "اطلب", "أطلب", "شراء",
        "اشتري", "أشتري", "حجز", "توصيل", "مقاس", "لون", "تفاصيل",
        "price", "available", "order", "buy", "purchase", "delivery",
        "how much", "can i get",
    )
    return any(marker in value for marker in markers)


def is_customer_order_intent(text: str):
    value = normalize_text(text)
    markers = (
        "اريد اطلب", "أريد أطلب", "ابغى اطلب", "أبغى أطلب",
        "اريد طلب", "أريد طلب", "ابغى اشتري", "أبغى أشتري",
        "كيف اطلب", "كيف أطلب", "طلب منتج", "تسجيل طلب",
        "اطلب", "أطلب", "شراء", "اشتري", "أشتري", "order", "buy",
        "purchase",
    )
    return any(marker in value for marker in markers)


async def process_telegram_update(update: dict):
    # Channel product posts are always import-only and never receive replies.
    source_post = update.get("channel_post") or update.get("edited_channel_post")
    if source_post:
        source_chat = source_post.get("chat") or {}
        source_chat_id = str(source_chat.get("id") or "")
        configured_chat_id = TELEGRAM_PRODUCTS_CHAT_ID

        # An optional env setting is only an allow-list for the external channel.
        # Accept either Telegram's numeric chat ID or its public username.
        # The merchant is ALWAYS resolved from the explicit DB mapping.
        if not telegram_chat_matches_configured(source_chat, configured_chat_id):
            return

        try:
            business_id = await asyncio.to_thread(
                get_channel_business_id, "telegram_products", source_chat_id
            )
            if not business_id:
                logger.error(
                    "Telegram product import skipped: no explicit store mapping for channel %s.",
                    source_chat_id,
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
                "Telegram product channel processed silently: chat_id=%s result=%s",
                source_chat_id,
                result,
            )
        except Exception:
            logger.exception("Failed to import a product from Telegram channel.")
        return

    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    chat_type = str(chat.get("type") or "unknown")

    # Group replies/imports are allowed only for groups explicitly mapped to a store.
    # An optional env chat ID narrows the allow-list; it never chooses a business.
    if chat_type in ("group", "supergroup") and telegram_chat_matches_configured(
        chat, TELEGRAM_PRODUCTS_CHAT_ID
    ):
        business_id = await asyncio.to_thread(
            get_channel_business_id, "telegram_products", chat_id
        )
        if not business_id:
            logger.info(
                "Telegram group ignored: no explicit store mapping for chat_id=%s",
                chat_id,
            )
            return

        sender = message.get("from") or {}
        sender_id = sender.get("id")
        if sender.get("is_bot") or sender_id is None:
            return

        is_admin = await asyncio.to_thread(
            is_telegram_group_admin, chat_id, sender_id
        )
        if is_admin is True:
            # Only verified group admins may publish catalog items. Import silently.
            try:
                result = await asyncio.to_thread(
                    import_telegram_product_post,
                    message,
                    supabase,
                    business_id,
                    TELEGRAM_PRODUCTS_CURRENCY,
                )
                logger.info(
                    "Telegram admin product post imported silently: sender=%s result=%s",
                    sender_id,
                    result,
                )
            except Exception:
                logger.exception("Failed to import a product from Telegram group.")
            return

        if is_admin is None:
            logger.info(
                "Telegram configured group message ignored safely: sender=%s admin_status=unknown",
                sender_id,
            )
            return

        user_text = str(message.get("text") or message.get("caption") or "").strip()
        # Product posts from customers are not imported; only admins can publish catalog items.
        if not is_customer_question_or_request(user_text):
            return

        sender_name = str(sender.get("first_name") or "عميل").strip()
        reply_to = message.get("message_id")
        if is_customer_order_intent(user_text):
            intro = (
                f"أهلًا {sender_name}، لإتمام طلبك وحفاظًا على خصوصية بياناتك، "
                "سنكمل معك عبر المحادثة الخاصة مع مساعد المتجر.\n\n"
                "1) افتح رابط مساعد المتجر الذي نشره صاحب المتجر.\n"
                "2) اضغط Start في Telegram.\n"
                "3) أرسل «أريد طلب» واتبع التعليمات لاختيار المنتج والكمية."
            )
            if TELEGRAM_BOT_USERNAME:
                private_link = (
                    f"https://t.me/{TELEGRAM_BOT_USERNAME}?start={business_id}"
                )
                reply_text = intro + f"\n\nرابط إتمام الطلب: {private_link}"
            else:
                # Never hard-code one merchant's bot username into all stores.
                reply_text = (
                    intro
                    + "\n\nلم يُضبط اسم مستخدم البوت في إعدادات النظام؛ "
                    "يرجى استخدام رابط مساعد المتجر الموجود في وصف المتجر."
                )
        else:
            context = await asyncio.to_thread(get_business_context, business_id)
            if not context:
                logger.error("Telegram group reply skipped: mapped store context unavailable.")
                return
            reply_text = await asyncio.to_thread(
                generate_ai_response,
                f"telegram_group_{chat_id}_{sender_id}",
                user_text,
                None,
                context,
                f"telegram_group_{chat_id}_{sender_id}_{business_id}",
                business_id,
            )

        await send_telegram_message(chat_id, reply_text)
        return

    # Private chat: bind the customer to the store from the bot deep-link,
    # then answer privately so order details and personal data are never public.
    sender = message.get("from") or {}
    private_chat_id = int(chat.get("id"))
    user_text = str(message.get("text") or message.get("caption") or "").strip()

    if user_text.startswith("/start"):
        parts = user_text.split(maxsplit=1)
        # A deep-link payload identifies the intended store. A bare /start may
        # reuse this customer's existing store binding, but must never choose a default store.
        business_id = parts[1].strip() if len(parts) > 1 else ""
        if not business_id:
            # A bare /start must not silently attach the customer to a default store.
            business_id = await asyncio.to_thread(
                get_telegram_business_id, private_chat_id
            ) or ""

        if business_id and await asyncio.to_thread(
            save_telegram_business, private_chat_id, business_id
        ):
            pending_order_cleared = await asyncio.to_thread(
                clear_pending_order_if_store_changed, private_chat_id, business_id
            )
            welcome_message = (
                "أهلًا بك في مساعد المتجر! 👋\n"
                "تم ربط هذه المحادثة بالمتجر الصحيح.\n\n"
                "لإتمام طلبك بأمان:\n"
                "1) أرسل «أريد طلب».\n"
                "2) اختر المنتج من القائمة.\n"
                "3) أرسل الكمية المطلوبة واتبع التعليمات حتى تأكيد الطلب.\n\n"
                "يمكنك أيضًا السؤال عن المنتجات والأسعار المتاحة.\n"
                "إذا رغبت في تذكير واحد بعد استفسارك، أرسل /reminders_on؛ ولن نرسل تذكيرات دون موافقتك."
            )
            if pending_order_cleared:
                welcome_message += (
                    "\n\nتم إلغاء الطلب غير المكتمل من المتجر السابق "
                    "لأنك انتقلت إلى متجر آخر. يمكنك بدء طلب جديد هنا."
                )
            await send_telegram_message(private_chat_id, welcome_message)
        else:
            logger.error(
                "Telegram /start could not bind customer: chat_id=%s payload_supplied=%s configured_business=%s",
                private_chat_id,
                len(parts) > 1,
                bool(business_id),
            )
            await send_telegram_message(
                private_chat_id,
                "تعذر ربط المحادثة بالمتجر حاليًا. يرجى إعادة فتح رابط المتجر والمحاولة مرة أخرى.",
            )
        return

    if user_text.startswith("/cancel"):
        clear_pending_telegram_order(private_chat_id)
        await send_telegram_message(private_chat_id, "تم إلغاء الطلب الجاري.")
        return

    # Reminders are strictly opt-in and can be stopped at any time.
    if user_text.strip().lower() in ("/reminders_on", "أوافق على التذكير", "أوافق على التذكيرات"):
        business_id = await asyncio.to_thread(get_telegram_business_id, private_chat_id)
        if not business_id:
            await send_telegram_message(private_chat_id, "افتح رابط المتجر واضغط Start أولًا.")
        elif await asyncio.to_thread(set_telegram_reminder_preference, business_id, private_chat_id, True):
            await send_telegram_message(private_chat_id, "تم تفعيل تذكير واحد فقط بعد استفسارك عن منتج، إذا لم تُكمل الطلب. لن نرسل تذكيرات متكررة، ويمكنك الإيقاف في أي وقت بإرسال /reminders_off.")
        else:
            await send_telegram_message(private_chat_id, "تعذر حفظ موافقتك حاليًا. حاول لاحقًا.")
        return

    if user_text.strip().lower() in ("/reminders_off", "إيقاف التذكيرات", "ايقاف التذكيرات", "لا ترسل تذكيرات"):
        business_id = await asyncio.to_thread(get_telegram_business_id, private_chat_id)
        if business_id:
            await asyncio.to_thread(set_telegram_reminder_preference, business_id, private_chat_id, False)
        await send_telegram_message(private_chat_id, "تم إيقاف التذكيرات لهذا المتجر.")
        return

    if user_text.strip().lower() in ("لا أريد تذكير", "لا اريد تذكير", "غير مهتم", "غير مهتمة", "ما عاد مهتم", "ما عدت مهتم", "توقف"):
        business_id = await asyncio.to_thread(get_telegram_business_id, private_chat_id)
        if business_id:
            await asyncio.to_thread(set_telegram_reminder_preference, business_id, private_chat_id, False)
        await send_telegram_message(private_chat_id, "تم إيقاف التذكيرات. لن نرسل تذكيرات متابعة أخرى لهذا المتجر.")
        return

    if user_text.strip().lower() in ("/points", "نقاطي", "رصيد النقاط", "نقاط الولاء"):
        business_id = await asyncio.to_thread(get_telegram_business_id, private_chat_id)
        if not business_id:
            await send_telegram_message(private_chat_id, "افتح رابط المتجر واضغط Start أولًا.")
            return
        settings = await asyncio.to_thread(get_store_growth_settings, business_id)
        if not settings.get("loyalty_enabled", True):
            await send_telegram_message(private_chat_id, "برنامج الولاء غير مفعّل في هذا المتجر حاليًا.")
            return
        account = await asyncio.to_thread(get_customer_loyalty, business_id, private_chat_id)
        if account is None:
            await send_telegram_message(private_chat_id, "تعذر قراءة نقاطك حاليًا. حاول لاحقًا.")
            return
        reply = format_loyalty_summary(
            account.get("points_balance", 0),
            account.get("lifetime_points", 0),
            settings.get("minimum_redeem_points", 100),
            settings.get("point_value_yer", 1),
        )
        reply += "\n\nتُضاف النقاط بعد تحويل الطلب إلى «تم التسليم» فقط. لاستبدال نقاطك أرسل /coupon متبوعًا برمز المكافأة."
        await send_telegram_message(private_chat_id, reply)
        return

    if user_text.strip().lower().startswith("/coupon "):
        business_id = await asyncio.to_thread(get_telegram_business_id, private_chat_id)
        parts = user_text.strip().split(maxsplit=1)
        coupon_code = parts[1].strip() if len(parts) > 1 else ""
        if not business_id or not coupon_code:
            await send_telegram_message(private_chat_id, "افتح رابط المتجر واضغط Start، ثم أرسل /coupon متبوعًا برمز المكافأة.")
            return
        try:
            redeemed = await asyncio.to_thread(
                lambda: supabase.rpc("redeem_store_loyalty_coupon", {
                    "p_business_id": business_id,
                    "p_customer_external_id": str(private_chat_id),
                    "p_coupon_code": coupon_code,
                }).execute().data
            )
            if isinstance(redeemed, list):
                redeemed = redeemed[0] if redeemed else {}
            if isinstance(redeemed, dict) and redeemed.get("success"):
                await send_telegram_message(
                    private_chat_id,
                    f"تم استبدال النقاط بنجاح! 🎉\nالمكافأة: {redeemed.get('title') or redeemed.get('code')}\nقيمة الخصم المسجلة: {redeemed.get('discount_amount')} {redeemed.get('currency') or 'YER'}\nتنتهي المكافأة: {redeemed.get('expires_at')}\nأظهر هذا الرمز للمتجر عند إتمام الشراء."
                )
            else:
                reason = redeemed.get("reason") if isinstance(redeemed, dict) else None
                message_text = "نقاطك غير كافية لاستبدال هذه المكافأة." if reason == "insufficient_points" else "رمز المكافأة غير صالح أو منتهي أو وصل إلى حد الاستخدام."
                await send_telegram_message(private_chat_id, message_text)
        except Exception:
            logger.exception("Failed to redeem store loyalty coupon.")
            await send_telegram_message(private_chat_id, "تعذر استبدال المكافأة الآن. تأكد من تطبيق ترحيل قاعدة البيانات وحاول لاحقًا.")
        return

    # A photo is a visual-search request even when it also has a caption.
    photos = message.get("photo") or []
    if photos:
        photo_file_id = str((photos[-1] or {}).get("file_id") or "")
        image_bytes = await asyncio.to_thread(download_telegram_photo, photo_file_id)
        if not image_bytes:
            await send_telegram_message(
                private_chat_id,
                "لم أتمكن من قراءة الصورة. أرسل صورة واضحة للمنتج مع إضاءة جيدة.",
            )
            return

        business_id = await asyncio.to_thread(get_telegram_business_id, private_chat_id)
        if not business_id:
            await send_telegram_message(
                private_chat_id,
                "للبحث عن المنتج بالصورة، افتح رابط مساعد المتجر واضغط Start أولًا حتى أبحث داخل كتالوج المتجر الصحيح.",
            )
            return

        products = await asyncio.to_thread(get_visual_search_products, business_id)
        if not products:
            await send_telegram_message(
                private_chat_id,
                "لا توجد منتجات متاحة للبحث البصري في هذا المتجر حاليًا.",
            )
            return

        try:
            matches = await asyncio.to_thread(
                find_products_by_image,
                image_bytes,
                products,
                user_text,
                business_id,
            )
        except Exception:
            logger.exception("Telegram visual product search failed.")
            matches = []

        if not matches:
            await send_telegram_message(
                private_chat_id,
                "لم أجد تطابقًا موثوقًا للصورة داخل منتجات هذا المتجر. "
                "جرّب صورة أوضح أو اكتب اسم المنتج أو لونه أو رقم موديله. "
                "لن أخمّن اسم المنتج أو سعره.",
            )
            return

        lines = ["وجدت منتجات قد تشبه الصورة في هذا المتجر:"]
        for item in matches:
            product = item["product"]
            name = str(product.get("name") or "منتج")
            model = str(product.get("model_number") or "").strip()
            price = product.get("price")
            currency = str(product.get("currency") or "YER")
            detail = f"• {name}"
            if model:
                detail += f" — الموديل: {model}"
            if price is not None:
                detail += f" — السعر المسجل: {price} {currency}"
            reason = str(item.get("reason") or "").strip()
            if reason:
                detail += f"\n  سبب التشابه: {reason}"
            lines.append(detail)
        lines.append("\nنتائج التشابه تقديرية وليست ضمانًا أن المنتج مطابق تمامًا للصورة.")
        await send_telegram_message(private_chat_id, "\n".join(lines))
        return

    if user_text:
        reply_text = await respond_to_telegram_text(private_chat_id, user_text)
        await send_telegram_message(private_chat_id, reply_text)
        return

    # Voice messages remain on the existing transcription flow; this stage adds
    # image search only and deliberately does not process media in public groups.
    if message.get("voice") or message.get("audio"):
        await send_telegram_message(
            private_chat_id,
            "وصلتني الرسالة الصوتية. أرسل سؤالك كتابةً أو استخدم البحث بالصورة لإيجاد منتج مشابه.",
        )
        return

    logger.info("Telegram private update contained no supported text.")


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

    durable_claim = await asyncio.to_thread(
        claim_telegram_update_durable, update_id
    )
    if durable_claim is False:
        finish_telegram_update(update_id)
        logger.info("Ignoring durable duplicate Telegram update (update_id=%s).", update_id)
        return {"ok": True, "duplicate": True}

    try:
        await process_telegram_update(update)
    except Exception:
        logger.exception("Failed to process Telegram webhook update; allowing retry.")
        if durable_claim is True:
            await asyncio.to_thread(release_telegram_update_durable, update_id)
        abandon_telegram_update(update_id)
        raise HTTPException(status_code=500, detail="Telegram update processing failed.")

    finish_telegram_update(update_id)
    return {"ok": True}


# -----------------------------
# Facebook Messenger webhook
# -----------------------------

def download_facebook_image(url: str):
    """Download a Facebook-hosted image only; reject arbitrary external URLs."""
    try:
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").lower()
        allowed_host = (
            host.endswith(".fbcdn.net")
            or host == "fbcdn.net"
            or host.endswith(".fbsbx.com")
            or host == "fbsbx.com"
            or host.endswith(".facebook.com")
            or host == "facebook.com"
        )
        if parsed.scheme != "https" or not allowed_host:
            logger.warning("Rejected Facebook attachment URL with an untrusted host.")
            return None

        data = bytearray()
        with requests.get(url, timeout=20, stream=True, allow_redirects=False) as response:
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                data.extend(chunk)
                if len(data) > 10 * 1024 * 1024:
                    logger.warning("Facebook image exceeded the 10 MB processing limit.")
                    return None
        content = bytes(data)
        if not (
            content.startswith(b"\xff\xd8\xff")
            or content.startswith(b"\x89PNG\r\n\x1a\n")
            or (len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP")
        ):
            logger.warning("Facebook attachment was not a supported image.")
            return None
        return content
    except requests.RequestException:
        logger.exception("Failed to download Facebook Messenger image.")
        return None


def send_facebook_message(recipient_id: str, text: str):
    """Send a reply through the single Facebook Page configured in Render."""
    if not FACEBOOK_PAGE_ACCESS_TOKEN or not FACEBOOK_PAGE_ID:
        logger.error("Facebook Messenger credentials are not configured.")
        return False

    endpoint = "https://graph.facebook.com/v26.0/me/messages"
    parts = [str(text or "عذرًا، لا يوجد رد حاليًا.")[i:i + 1900]
             for i in range(0, len(str(text or "عذرًا، لا يوجد رد حاليًا.")), 1900)]
    try:
        for part in parts:
            response = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {FACEBOOK_PAGE_ACCESS_TOKEN}"},
                json={
                    "recipient": {"id": str(recipient_id)},
                    "messaging_type": "RESPONSE",
                    "message": {"text": part},
                },
                timeout=20,
            )
            response.raise_for_status()
        return True
    except requests.RequestException:
        # Avoid logging request headers or access tokens.
        logger.exception("Failed to send Facebook Messenger reply.")
        return False


@app.get("/facebook/webhook")
async def verify_facebook_webhook(request: Request):
    params = request.query_params
    if (
        FACEBOOK_VERIFY_TOKEN
        and params.get("hub.mode") == "subscribe"
        and hmac.compare_digest(
            params.get("hub.verify_token", ""),
            FACEBOOK_VERIFY_TOKEN,
        )
    ):
        return Response(
            content=params.get("hub.challenge", ""),
            media_type="text/plain",
        )
    raise HTTPException(status_code=403, detail="Facebook webhook verification failed.")


@app.post("/facebook/webhook")
async def receive_facebook_webhook(request: Request):
    if not META_APP_SECRET:
        raise HTTPException(status_code=503, detail="Meta signature verification is not configured.")

    raw_body = await request.body()
    supplied_signature = request.headers.get("X-Hub-Signature-256", "").strip()
    expected_signature = "sha256=" + hmac.new(
        META_APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    if not supplied_signature or not hmac.compare_digest(supplied_signature, expected_signature):
        raise HTTPException(status_code=401, detail="Invalid Facebook webhook signature.")

    try:
        body = json.loads(raw_body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    if body.get("object") != "page":
        return {"status": "ignored"}

    # This initial Messenger connector intentionally supports one Page token
    # configured in Render. Other Pages are ignored rather than cross-routing tenants.
    configured_page_id = str(FACEBOOK_PAGE_ID or "").strip()
    if not configured_page_id or not FACEBOOK_PAGE_ACCESS_TOKEN:
        logger.warning("Facebook Messenger event ignored: Page credentials are not configured.")
        return {"status": "not_configured"}

    for entry in body.get("entry", []):
        page_id = str(entry.get("id") or "").strip()
        if page_id != configured_page_id:
            logger.warning("Ignoring Facebook event for an unconfigured Page ID.")
            continue

        business_id = await asyncio.to_thread(
            get_channel_business_id, "facebook_messenger", page_id
        )
        if not business_id:
            logger.warning("Facebook Page is not mapped to a merchant store.")
            continue

        context = await asyncio.to_thread(get_business_context, business_id)
        for event in entry.get("messaging", []):
            sender_id = str((event.get("sender") or {}).get("id") or "").strip()
            message = event.get("message") or {}
            if not sender_id or not message or message.get("is_echo"):
                continue
            # Delivery/read receipts and postbacks are not customer messages.
            if event.get("delivery") or event.get("read") or event.get("postback"):
                continue

            user_text = str(message.get("text") or "").strip()
            image_bytes = None
            attachments = message.get("attachments") or []
            for attachment in attachments:
                if (attachment.get("type") or "").lower() == "image":
                    payload = attachment.get("payload") or {}
                    image_bytes = await asyncio.to_thread(
                        download_facebook_image, payload.get("url", "")
                    )
                    if not image_bytes:
                        await asyncio.to_thread(
                            send_facebook_message,
                            sender_id,
                            "وصلتني الصورة، لكن تعذّر تحميلها. أرسل صورة أخرى أو اكتب سؤالك.",
                        )
                        break
                    if not user_text:
                        user_text = "حلّل صورة العميل وساعده اعتمادًا على كتالوج المتجر وأسعاره الفعلية."
                    break

            if attachments and any((a.get("type") or "").lower() == "image" for a in attachments) and not image_bytes:
                continue
            if not user_text and not image_bytes:
                continue

            try:
                reply = await asyncio.to_thread(
                    generate_ai_response,
                    f"facebook_{sender_id}",
                    user_text,
                    image_bytes,
                    context,
                    f"facebook_{page_id}_{sender_id}_{business_id}",
                    business_id,
                )
                await asyncio.to_thread(send_facebook_message, sender_id, reply)
            except Exception:
                logger.exception("Failed to process Facebook Messenger customer message.")

    return {"status": "ok"}


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
    # Verify Meta's signature against the exact raw request body before parsing
    # or processing any event. Never accept unsigned requests in production.
    if not META_APP_SECRET:
        logger.error("META_APP_SECRET is not configured; rejecting WhatsApp webhook.")
        raise HTTPException(
            status_code=503,
            detail="Webhook signature verification is not configured.",
        )

    raw_body = await request.body()
    supplied_signature = request.headers.get("X-Hub-Signature-256", "").strip()
    expected_signature = "sha256=" + hmac.new(
        META_APP_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    if not supplied_signature or not hmac.compare_digest(
        supplied_signature,
        expected_signature,
    ):
        logger.warning("Rejected WhatsApp webhook with missing or invalid signature.")
        raise HTTPException(
            status_code=401,
            detail="Invalid webhook signature.",
        )

    try:
        import json
        body = json.loads(raw_body)
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
                        business_id,
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


test_ai_security = HTTPBasic(auto_error=False)


@app.post("/test-ai")
async def test_ai(
    request: TestAIRequest,
    credentials: HTTPBasicCredentials = Depends(test_ai_security),
):
    # This diagnostic endpoint can incur provider costs, so restrict it to the
    # configured administrator rather than leaving it publicly callable.
    admin_username = os.getenv("ADMIN_USERNAME", "")
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    if not admin_username or not admin_password:
        raise HTTPException(
            status_code=503,
            detail="إعداد بيانات دخول المدير غير مكتمل",
        )
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="يلزم تسجيل دخول المدير",
            headers={"WWW-Authenticate": "Basic"},
        )
    valid_user = hmac.compare_digest(credentials.username, admin_username)
    valid_password = hmac.compare_digest(credentials.password, admin_password)
    if not (valid_user and valid_password):
        raise HTTPException(
            status_code=401,
            detail="بيانات دخول المدير غير صحيحة",
            headers={"WWW-Authenticate": "Basic"},
        )

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
