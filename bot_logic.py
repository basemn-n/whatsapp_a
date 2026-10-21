import os
import base64
import threading
import uuid

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_community.chat_message_histories import ChatMessageHistory

load_dotenv()

# معلومات المتجر الافتراضية، للتوافق مع البوت الحالي
company_knowledge = ""

if os.path.exists("company_knowledge.txt"):
    with open("company_knowledge.txt", "r", encoding="utf-8") as f:
        company_knowledge = f.read()

# ذاكرة المحادثات داخل الخادم
store = {}
store_lock = threading.Lock()


def get_session_history(session_id: str) -> ChatMessageHistory:
    with store_lock:
        if session_id not in store:
            store[session_id] = ChatMessageHistory()
        return store[session_id]


api_key = os.getenv("GROQ_API_KEY")

if not api_key:
    raise RuntimeError("GROQ_API_KEY غير موجود في إعدادات Render.")

llm = ChatOpenAI(
    model="openai/gpt-oss-120b",
    temperature=0.3,
    api_key=api_key,
    base_url="https://api.groq.com/openai/v1",
)

# نموذج منفصل يدعم الصور؛ نموذج GPT-OSS النصي لا يستقبل الصور.
vision_llm = ChatOpenAI(
    model=os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b"),
    temperature=0.3,
    api_key=api_key,
    base_url="https://api.groq.com/openai/v1",
)


def extract_token_usage(response):
    """Return provider-reported token counts without retaining message contents."""
    metadata = getattr(response, "usage_metadata", None) or {}
    if not metadata:
        response_metadata = getattr(response, "response_metadata", None) or {}
        metadata = response_metadata.get("token_usage") or response_metadata.get("usage") or {}

    def non_negative_int(*keys):
        for key in keys:
            value = metadata.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return 0

    input_tokens = non_negative_int("input_tokens", "prompt_tokens")
    output_tokens = non_negative_int("output_tokens", "completion_tokens")
    total_tokens = non_negative_int("total_tokens") or input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def log_ai_usage(response, operation, business_id=None):
    """Persist token counts only; never send customer messages, phone numbers, or chat IDs."""
    import logging
    import requests
    logger = logging.getLogger(__name__)
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not service_key:
        logger.info("AI usage not persisted because Supabase service settings are unavailable.")
        return

    usage = extract_token_usage(response)
    response_metadata = getattr(response, "response_metadata", None) or {}
    default_model = (
        os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")
        if operation == "image"
        else os.getenv("GROQ_TEXT_MODEL", "openai/gpt-oss-120b")
    )
    model_name = response_metadata.get("model_name") or response_metadata.get("model") or default_model
    rates = {
        "input": os.getenv("GROQ_INPUT_COST_PER_MILLION_USD"),
        "output": os.getenv("GROQ_OUTPUT_COST_PER_MILLION_USD"),
    }
    estimated_cost = None
    try:
        if rates["input"] is not None and rates["output"] is not None:
            input_rate = float(rates["input"])
            output_rate = float(rates["output"])
            if input_rate >= 0 and output_rate >= 0:
                estimated_cost = (
                    usage["input_tokens"] * input_rate
                    + usage["output_tokens"] * output_rate
                ) / 1_000_000
    except (TypeError, ValueError):
        estimated_cost = None

    record = {
        "provider": "groq",
        "model": str(model_name)[:120],
        "operation": operation,
        "business_id": business_id if isinstance(business_id, str) and _is_uuid(business_id) else None,
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "total_tokens": usage["total_tokens"],
        "estimated_cost_usd": estimated_cost,
    }
    try:
        response_http = requests.post(
            f"{supabase_url}/rest/v1/ai_usage_logs",
            headers={
                "apikey": service_key,
                "Authorization": f"Bearer {service_key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            json=record,
            timeout=5,
        )
        if not response_http.ok:
            logger.warning("Could not persist AI usage metrics (HTTP %s).", response_http.status_code)
    except requests.RequestException:
        logger.warning("Could not persist AI usage metrics due to a network error.")


def _is_uuid(value):
    try:
        uuid.UUID(value)
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def generate_ai_response(
    user_phone: str,
    user_message: str,
    image_bytes: bytes = None,
    business_context: str = None,
    history_key: str = None,
    business_id: str = None,
) -> str:
    """
    business_context: معلومات النشاط التجاري الذي يتواصل معه العميل.
    history_key: معرف مستقل للمحادثة حتى لا تختلط محادثات العملاء.
    """

    # Never fall back to a global company file for a tenant conversation:
    # that could disclose another store's products or policies.
    context = (
        business_context.strip()
        if isinstance(business_context, str) and business_context.strip()
        else "معلومات النشاط التجاري غير متوفرة حاليًا. لا تؤكد أي أسعار أو منتجات."
    )

    system_prompt = (
        "أنت مساعد خدمة عملاء ذكي ومؤدب وودود.\n"
        "أجب باللغة العربية بأسلوب احترافي ومختصر.\n"
        "اعتمد على معلومات النشاط التجاري المرفقة فقط.\n"
        "لا تخترع أسعارًا أو سياسات أو معلومات غير موجودة.\n"
        "إذا لم تجد المعلومة، فأخبر العميل أنك تحتاج إلى التأكد منها.\n"
        "لا تكشف معلومات داخلية أو بيانات تخص عملاء آخرين.\n"
        "لا تخترع تطابقًا بين صورة ومنتج في المتجر. لا تؤكد اسم منتج أو سعره أو توفره "
        "إلا إذا كان المنتج نفسه مذكورًا بوضوح في بيانات النشاط التجاري المرفقة.\n"
        "إذا لم توجد بيانات متجر كافية أو لم تستطع مطابقة المنتج بدقة، صف ما تراه في الصورة "
        "فقط ووضّح أنك لا تستطيع تأكيد أنه منتج المتجر أو تحديد سعره من الصورة وحدها.\n\n"
        "معلومات النشاط التجاري:\n"
        f"{context}"
    )

    session_id = history_key or user_phone
    history = get_session_history(session_id)

    messages = [SystemMessage(content=system_prompt)]
    messages.extend(history.messages)

    if image_bytes:
        base64_image = base64.b64encode(image_bytes).decode("utf-8")
        user_content = [
            {
                "type": "text",
                "text": user_message or "يرجى وصف الصورة ومساعدة العميل بشأنها.",
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{base64_image}"
                },
            },
        ]
        messages.append(HumanMessage(content=user_content))
    else:
        messages.append(HumanMessage(content=user_message))

    response = (vision_llm if image_bytes else llm).invoke(messages)
    log_ai_usage(response, "image" if image_bytes else "text", business_id)
    answer = response.content

    if not isinstance(answer, str):
        answer = str(answer)

    history.add_user_message(user_message or "[صورة مرفقة]")
    history.add_ai_message(answer)

    return answer


def transcribe_audio(audio_file_path: str) -> str:
    """Transcribe an audio file to its spoken language using Groq Whisper."""
    import mimetypes
    import requests

    if not api_key:
        raise RuntimeError("GROQ_API_KEY غير مضبوط.")

    if not os.path.isfile(audio_file_path):
        raise FileNotFoundError("ملف الصوت غير موجود.")

    file_size = os.path.getsize(audio_file_path)
    if file_size == 0:
        raise ValueError("ملف الصوت فارغ.")
    if file_size > 25 * 1024 * 1024:
        raise ValueError("حجم الملف الصوتي يتجاوز حد Groq البالغ 25 ميجابايت.")

    model = os.getenv("GROQ_TRANSCRIPTION_MODEL", "whisper-large-v3-turbo")
    filename = os.path.basename(audio_file_path)
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    endpoint = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"}
    data = {
        "model": model,
        "language": "ar",
        "response_format": "json",
        "temperature": "0",
        "prompt": (
            "الكلام باللغة العربية. احتفظ بأسماء المنتجات والأرقام والأسعار "
            "وأسماء الأشخاص كما نطقها المتحدث قدر الإمكان."
        ),
    }

    try:
        with open(audio_file_path, "rb") as audio_file:
            response = requests.post(
                endpoint,
                headers=headers,
                data=data,
                files={"file": (filename, audio_file, mime_type)},
                timeout=120,
            )
        if not response.ok:
            # Do not include request headers or credentials in logs/errors.
            detail = ""
            try:
                detail = str(response.json().get("error", {}).get("message", ""))
            except (ValueError, AttributeError):
                detail = response.text[:300]
            raise RuntimeError(
                f"Groq transcription failed (HTTP {response.status_code}): {detail}"
            )

        result = response.json()
        transcript = str(result.get("text") or "").strip()
        if not transcript:
            raise ValueError("لم يتم التعرف على كلام واضح في الرسالة الصوتية.")
        return transcript

    except requests.RequestException as exc:
        raise RuntimeError("تعذر الاتصال بخدمة تحويل الصوت إلى نص.") from exc
