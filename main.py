import os
import asyncio
import logging
import requests
import tempfile

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


# -----------------------------
# WhatsApp functions
# -----------------------------

def send_whatsapp_message(to_phone: str, message_text: str):
    """إرسال رد نصي إلى العميل عبر WhatsApp."""
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
            url, json=data, headers=headers, timeout=20
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("فشل إرسال رسالة WhatsApp: %s", exc)


def download_whatsapp_media(media_id: str):
    """تحميل صورة أو ملف صوتي من WhatsApp."""
    if not WHATSAPP_TOKEN or not media_id:
        return None

    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    endpoint = f"https://graph.facebook.com/v19.0/{media_id}"

    try:
        response = requests.get(
            endpoint, headers=headers, timeout=20
        )
        response.raise_for_status()
        media_url = response.json().get("url")

        if not media_url:
            return None

        media_response = requests.get(
            media_url, headers=headers, timeout=30
        )
        media_response.raise_for_status()
        return media_response.content

    except requests.RequestException as exc:
        logger.error("فشل تحميل وسائط WhatsApp: %s", exc)
        return None


# -----------------------------
# Telegram functions
# -----------------------------

def telegram_api(method: str, payload=None, timeout=20):
    """استدعاء Telegram Bot API."""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN غير موجود في إعدادات Render."
        )

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/{method}"
    )

    response = requests.post(
        url, json=payload or {}, timeout=timeout
    )
    response.raise_for_status()

    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API error: {result}"
        )

    return result.get("result")


async def telegram_polling():
    """استقبال الرسائل الجديدة من Telegram."""
    if not TELEGRAM_BOT_TOKEN:
        logger.error(
            "TELEGRAM_BOT_TOKEN غير موجود؛ "
            "سيتم تعطيل استقبال Telegram."
        )
        return

    offset = 0
    logger.info("بدأ استقبال رسائل Telegram.")

    # تأكد أن الرمز صالح وأنه لا توجد Webhook قديمة.
    try:
        bot_info = await asyncio.to_thread(
            telegram_api, "getMe"
        )
        logger.info(
            "Telegram bot connected: @%s",
            bot_info.get("username", "unknown"),
        )
    except Exception:
        logger.exception(
            "تعذر الاتصال بـ Telegram. "
            "تحقق من الرمز وإعدادات الشبكة."
        )
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
                user_text = message.get("text", "").strip()

                if not chat_id or not user_text:
                    continue

                if user_text.startswith("/start"):
                    reply = (
                        "مرحبًا بك! 👋\n"
                        "أنا مساعد خدمة العملاء الذكي.\n"
                        "أرسل سؤالك وسأساعدك قدر الإمكان."
                    )

                elif user_text.startswith("/"):
                    reply = (
                        "أرسل سؤالك في رسالة نصية "
                        "وسأحاول مساعدتك."
                    )

                else:
                    try:
                        reply = await asyncio.to_thread(
                            generate_ai_response,
                            f"telegram_{chat_id}",
                            user_text,
                        )
                    except Exception:
                        logger.exception(
                            "فشل توليد رد الذكاء الاصطناعي."
                        )
                        reply = (
                            "عذرًا، حدث خطأ مؤقت. "
                            "يرجى المحاولة مرة أخرى."
                        )

                # Telegram يسمح بنصوص تصل إلى 4096 حرفًا.
                reply = str(reply or "عذرًا، لا يوجد رد حاليًا.")
                chunks = [
                    reply[i:i + 4000]
                    for i in range(0, len(reply), 4000)
                ]

                for chunk in chunks:
                    try:
                        await asyncio.to_thread(
                            telegram_api,
                            "sendMessage",
                            {
                                "chat_id": chat_id,
                                "text": chunk,
                            },
                            20,
                        )
                    except Exception:
                        logger.exception(
                            "فشل إرسال الرد إلى Telegram."
                        )
                        break

        except asyncio.CancelledError:
            logger.info("إيقاف استقبال رسائل Telegram.")
            raise

        except Exception:
            logger.exception(
                "حدث خطأ أثناء استقبال رسائل Telegram."
            )
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
# WhatsApp webhook verification
# -----------------------------

@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return Response(
            content=challenge or "",
            media_type="text/plain",
        )

    raise HTTPException(
        status_code=403,
        detail="Verification failed",
    )


# -----------------------------
# Receive WhatsApp messages
# -----------------------------

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

                for message in value.get("messages", []):
                    from_number = message.get("from")
                    msg_type = message.get("type")

                    if not from_number:
                        continue

                    if msg_type == "text":
                        user_text = message.get(
                            "text", {}
                        ).get("body", "")

                        if user_text:
                            ai_reply = await asyncio.to_thread(
                                generate_ai_response,
                                from_number,
                                user_text,
                            )
                            await asyncio.to_thread(
                                send_whatsapp_message,
                                from_number,
                                ai_reply,
                            )

                    elif msg_type == "image":
                        image_data = message.get("image", {})
                        image_id = image_data.get("id")

                        user_text = image_data.get(
                            "caption", "يرجى وصف الصورة."
                        )
                        image_bytes = await asyncio.to_thread(
                            download_whatsapp_media,
                            image_id,
                        )

                        if image_bytes:
                            ai_reply = await asyncio.to_thread(
                                generate_ai_response,
                                from_number,
                                user_text,
                                image_bytes,
                            )
                            await asyncio.to_thread(
                                send_whatsapp_message,
                                from_number,
                                ai_reply,
                            )

                    elif msg_type in ("audio", "voice"):
                        audio_data = message.get(
                            msg_type, {}
                        )
                        audio_id = audio_data.get("id")

                        audio_bytes = await asyncio.to_thread(
                            download_whatsapp_media,
                            audio_id,
                        )

                        if audio_bytes:
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

                                ai_reply = await asyncio.to_thread(
                                    generate_ai_response,
                                    from_number,
                                    user_text,
                                )
                                await asyncio.to_thread(
                                    send_whatsapp_message,
                                    from_number,
                                    ai_reply,
                                )

                            except Exception:
                                logger.exception(
                                    "فشل معالجة رسالة صوتية."
                                )
                            finally:
                                if temp_path and os.path.exists(
                                    temp_path
                                ):
                                    os.remove(temp_path)

    except Exception:
        logger.exception(
            "حدث خطأ أثناء معالجة Webhook الخاص بواتساب."
        )

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
        return {"success": True, "reply": reply}

    except Exception:
        logger.exception("فشل اختبار الذكاء الاصطناعي.")
        raise HTTPException(
            status_code=500,
            detail="فشل اختبار الذكاء الاصطناعي. "
                   "راجع سجلات Render لمعرفة السبب.",
        )


# -----------------------------
# Basic health check
# -----------------------------

@app.get("/")
async def home():
    return {
        "status": "running",
        "message": "Customer service bot is running.",
    }
