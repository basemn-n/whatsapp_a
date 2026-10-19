import os
import base64
import threading

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


def generate_ai_response(
    user_phone: str,
    user_message: str,
    image_bytes: bytes = None,
    business_context: str = None,
    history_key: str = None,
) -> str:
    """
    business_context: معلومات النشاط التجاري الذي يتواصل معه العميل.
    history_key: معرف مستقل للمحادثة حتى لا تختلط محادثات العملاء.
    """

    context = (
        business_context.strip()
        if isinstance(business_context, str) and business_context.strip()
        else company_knowledge or "معلومات النشاط التجاري غير متوفرة حاليًا."
    )

    system_prompt = (
        "أنت مساعد خدمة عملاء ذكي ومؤدب وودود.\n"
        "أجب باللغة العربية بأسلوب احترافي ومختصر.\n"
        "اعتمد على معلومات النشاط التجاري المرفقة فقط.\n"
        "لا تخترع أسعارًا أو سياسات أو معلومات غير موجودة.\n"
        "إذا لم تجد المعلومة، فأخبر العميل أنك تحتاج إلى التأكد منها.\n"
        "لا تكشف معلومات داخلية أو بيانات تخص عملاء آخرين.\n\n"
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
    answer = response.content

    if not isinstance(answer, str):
        answer = str(answer)

    history.add_user_message(user_message or "[صورة مرفقة]")
    history.add_ai_message(answer)

    return answer


def transcribe_audio(audio_file_path: str) -> str:
    raise RuntimeError(
        "تحويل الصوت غير مفعّل حاليًا. يمكن للبوت استقبال النصوص بعد نجاح اختبار الاتصال."
    )
