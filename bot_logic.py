import os
import base64

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_community.chat_message_histories import ChatMessageHistory

load_dotenv()

# تحميل معلومات الشركة دون استخدام OpenAI Embeddings
company_knowledge = ""

if os.path.exists("company_knowledge.txt"):
    with open("company_knowledge.txt", "r", encoding="utf-8") as f:
        company_knowledge = f.read()

# ذاكرة المحادثات
store = {}


def get_session_history(session_id: str) -> ChatMessageHistory:
    if session_id not in store:
        store[session_id] = ChatMessageHistory()
    return store[session_id]


# إعداد Vercel AI Gateway
api_key = os.getenv("AI_GATEWAY_API_KEY")

if not api_key:
    raise RuntimeError(
        "AI_GATEWAY_API_KEY غير موجود في إعدادات Render."
    )

llm = ChatOpenAI(
    model="nvidia/nemotron-3.5-lightning-free",
    temperature=0.3,
    api_key=api_key,
    base_url="https://ai-gateway.vercel.sh/v1",
)

# تعليمات خدمة العملاء
system_prompt = (
    "أنت مساعد خدمة عملاء ذكي ومؤدب وودود.\n"
    "تحدث باللغة العربية بأسلوب احترافي ومختصر.\n"
    "أجب عن أسئلة الشركة اعتمادًا على المعلومات المرفقة أدناه.\n"
    "لا تخترع أسعارًا أو سياسات أو تفاصيل غير موجودة.\n"
    "إذا لم تجد المعلومة، أخبر العميل أنك تحتاج إلى التأكد منها.\n\n"
    "معلومات الشركة:\n"
    + (company_knowledge or "معلومات الشركة غير متوفرة حاليًا.")
)


def generate_ai_response(
    user_phone: str,
    user_message: str,
    image_bytes: bytes = None
) -> str:
    history = get_session_history(user_phone)

    # إضافة تعليمات الشركة وسجل المحادثة
    messages = [SystemMessage(content=system_prompt)]
    messages.extend(history.messages)

    if image_bytes:
        base64_image = base64.b64encode(image_bytes).decode("utf-8")

        user_content = [
            {
                "type": "text",
                "text": user_message or
                "يرجى وصف الصورة ومساعدة العميل بشأنها."
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{base64_image}"
                }
            }
        ]

        messages.append(HumanMessage(content=user_content))
    else:
        messages.append(HumanMessage(content=user_message))

    response = llm.invoke(messages)
    answer = response.content

    if not isinstance(answer, str):
        answer = str(answer)

    history.add_user_message(
        user_message or "[صورة مرفقة]"
    )
    history.add_ai_message(answer)

    return answer


def transcribe_audio(audio_file_path: str) -> str:
    """
    تحويل الرسالة الصوتية إلى نص باستخدام OpenAI Whisper.
    هذه الوظيفة تحتاج إلى مفتاح OpenAI صالح ورصيد API.
    """

    openai_api_key = os.getenv("OPENAI_API_KEY")

    if not openai_api_key:
        raise RuntimeError(
            "تحويل الصوت يتطلب OPENAI_API_KEY في إعدادات Render."
        )

    from openai import OpenAI

    client = OpenAI(api_key=openai_api_key)

    with open(audio_file_path, "rb") as audio_file:
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="ar"
        )

    return transcript.text
