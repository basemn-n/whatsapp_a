
import os
import re
import asyncio
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

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

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

# حالة الطلب المؤقتة لكل محادثة.
# الطلبات غير المكتملة تُفقد عند إعادة تشغيل الخدمة.
pending_telegram_orders = {}
order_lock = threading.Lock()


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
        str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
    )

    match = re.search(r"\d+", digits)

    if not match:
        return None

    quantity = int(match.group())

    if quantity < 1 or quantity > 1000:
        return None

    return quantity


def save_telegram_order(chat_id: int, order_data: dict):
    """حفظ طلب مؤكد في جدول orders وإرجاع بياناته."""

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
        # إعادة التأكد من أن النشاط التجاري موجود.
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

        # إعادة التأكد من أن المنتج ما زال متاحًا.
        product_result = (
            supabase.table("products")
            .select("name, is_available")
            .eq("business_id", business_id)
            .eq("name", order_data["product_name"])
            .eq("is_available", True)
            .limit(1)
            .execute()
        )

        if not product_result.data:
            logger.warning("المنتج لم يعد متاحًا؛ لم يُحفظ الطلب.")
            return None

        result = (
            supabase.table("orders")
            .insert({
                "business_id": business_id,
                "customer_name": order_data["customer_name"].strip(),
                "customer_phone": order_data["customer_phone"].strip(),
                "customer_address": order_data["customer_address"].strip(),
                "payment_method": order_data["payment_method"].strip(),
                "product_name": product_result.data[0]["name"],
                "quantity": quantity,
                "status": "pending",
            })
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

    with order_lock:
        pending_telegram_orders[chat_id] = {
            "business_id": business_id,
            "stage": "quantity" if selected else "product",
            "product_name": selected.get("name") if selected else None,
            "quantity": 1,
        }

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

    if normalized in ("/cancel", "إلغاء", "الغاء", "الغاء الطلب", "إلغاء الطلب"):
        with order_lock:
            pending_telegram_orders.pop(chat_id, None)
        return "تم إلغاء الطلب الجاري. يمكنك البدء من جديد متى شئت."

    with order_lock:
        state = pending_telegram_orders.get(chat_id)

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

    # لا تسمح بتغيير النشاط المرتبط بالطلب الجاري.
    if not state.get("business_id"):
        with order_lock:
            pending_telegram_orders.pop(chat_id, None)
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

        return "ما اسمك الكامل؟"

    if stage == "customer_name":
        if len(text) < 2 or len(text) > 150:
            return "يرجى إدخال الاسم بصورة صحيحة."

        with order_lock:
            state["customer_name"] = text
            state["stage"] = "customer_phone"

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

        return "ما عنوان التوصيل الكامل؟ اذكر المدينة والمنطقة والشارع."

    if stage == "customer_address":
        if len(text) < 5 or len(text) > 500:
            return "يرجى إدخال عنوان توصيل أوضح."

        with order_lock:
            state["customer_address"] = text
            state["stage"] = "payment_method"

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
        yes_values = ("نعم", "أؤكد", "اكد", "تأكيد", "تأكيد الطلب", "yes", "confirm")
        no_values = ("لا", "إلغاء", "الغاء", "لا أريد", "no", "cancel")

        if normalized in yes_values:
            saved = save_telegram_order(chat_id, state)

            if not saved:
                return (
                    "لم أتمكن من تسجيل الطلب في قاعدة البيانات، "
                    "لذلك لم يتم تأكيده. يرجى المحاولة لاحقًا أو التواصل "
                    "مع خدمة العملاء."
                )

            with order_lock:
                pending_telegram_orders.pop(chat_id, None)

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

    with order_lock:
        pending_telegram_orders.pop(chat_id, None)

    return "انتهت حالة الطلب. أرسل «أريد طلب» للبدء من جديد."


# -----------------------------
# WhatsApp business context
# -----------------------------

def get_whatsapp_business_context():
    if not supabase or not PHONE_NUMBER_ID:
        return None

    try:
        result = (
            supabase.table("channel_connections")
            .select("business_id")
            .eq("channel", "whatsapp")
            .eq("external_account_id", PHONE_NUMBER_ID)
            .limit(1)
            .execute()
        )

        if not result.data:
            return None

        business_id = result.data[0].get("business_id")

        if not business_id:
            return None

        return get_business_context(business_id)

    except Exception:
        logger.exception("تعذر ربط WhatsApp بالنشاط.")
        return None


# -----------------------------
# WhatsApp functions
# -----------------------------

def send_whatsapp_message(to_phone: str, message_text: str):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        logger.error("إعدادات WhatsApp غير مكتملة.")
        return

    url = (
        f"https://graph.facebook.com/v19.0/"
        f"{PHONE_NUMBER_ID}/messages"
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


async def telegram_polling():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("Telegram غير مفعّل: الرمز غير موجود.")
        return

    offset = 0
    logger.info("بدأ استقبال رسائل Telegram.")

    try:
        bot_info = await asyncio.to_thread(
            telegram_api,
            "getMe",
        )

        logger.info(
            "Telegram bot connected: @%s",
            bot_info.get("username", "unknown"),
        )

    except Exception:
        logger.exception("تعذر الاتصال بـ Telegram.")
        return

    while True:
        try:
            updates = await asyncio.to_thread(
                telegram_api,
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 25,
                    "allowed_updates": ["message"],
                },
                35,
            )

            for update in updates or []:
                offset = update["update_id"] + 1

                message = update.get("message", {})
                chat = message.get("chat", {})
                chat_id = chat.get("id")

                user_text = (
                    message.get("text") or ""
                ).strip()

                if not chat_id or not user_text:
                    continue

                if user_text.startswith("/start"):
                    parts = user_text.split(maxsplit=1)
                    payload = parts[1].strip() if len(parts) > 1 else ""

                    business_id = None

                    if payload:
                        try:
                            business_id = str(uuid.UUID(payload))
                        except ValueError:
                            business_id = None

                    if (
                        business_id
                        and save_telegram_business(chat_id, business_id)
                    ):
                        reply = (
                            "مرحبًا بك! 👋\n"
                            "تم ربط هذه المحادثة بالنشاط التجاري.\n"
                            "أرسل سؤالك وسأساعدك."
                        )
                    else:
                        reply = (
                            "مرحبًا بك! 👋\n"
                            "أنا مساعد خدمة العملاء الذكي.\n"
                            "يمكنك إرسال سؤالك في أي وقت."
                        )

                elif user_text.startswith("/cancel"):
                    with order_lock:
                        pending_telegram_orders.pop(chat_id, None)
                    reply = "تم إلغاء الطلب الجاري."

                else:
                    try:
                        business_id = get_telegram_business_id(chat_id)

                        # إدارة الطلبات خارج الذكاء الاصطناعي
                        # حتى لا يدّعي البوت تسجيل طلب غير محفوظ.
                        order_reply = handle_telegram_order(
                            chat_id,
                            user_text,
                            business_id,
                        )

                        if order_reply is not None:
                            reply = order_reply
                        elif user_text.startswith("/"):
                            reply = "الأوامر المتاحة: /start و /cancel"
                        else:
                            context = (
                                get_business_context(business_id)
                                if business_id
                                else None
                            )

                            reply = await asyncio.to_thread(
                                generate_ai_response,
                                f"telegram_{chat_id}",
                                user_text,
                                None,
                                context,
                                (
                                    f"telegram_{chat_id}_"
                                    f"{business_id or 'default'}"
                                ),
                            )

                    except Exception:
                        logger.exception(
                            "فشل معالجة رسالة Telegram."
                        )
                        reply = (
                            "عذرًا، حدث خطأ مؤقت. "
                            "يرجى المحاولة مرة أخرى."
                        )

                try:
                    await send_telegram_message(chat_id, reply)
                except Exception:
                    logger.exception("فشل إرسال رد Telegram.")

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception("حدث خطأ في استقبال Telegram.")
            await asyncio.sleep(5)


# -----------------------------
# FastAPI lifecycle
# -----------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    telegram_task = asyncio.create_task(telegram_polling())
    app.state.telegram_task = telegram_task

    try:
        yield
    finally:
        telegram_task.cancel()

        try:
            await telegram_task
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=lifespan)


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
        context = await asyncio.to_thread(
            get_whatsapp_business_context
        )

        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                for message in value.get("messages", []):
                    from_number = message.get("from")
                    msg_type = message.get("type")

                    if not from_number:
                        continue

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
