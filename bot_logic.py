
import os
import base64

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_community.chat_message_histories import ChatMessageHistory

load_dotenv()

# تحميل معلومات الشركة
company_knowledge = ""

if os.path.exists("company_knowledge.txt"):
    with open("company_knowledge.txt", "r", encoding="utf-8") as f:
        company_knowledge = f.read()

# إعداد ذاكرة المحادثات
store = {}


def get_session_history(session_id: str) -> ChatMessageHistory:
    if session_id not in store:
        store[session_id] = ChatMessageHistory()
    return store[session_id]


# إعداد نموذج المحادثة عبر Vercel AI Gateway
api_key = os.getenv("AI_GATEWAY_API_KEY")

if not api_key:
    raise RuntimeError(
        "متغير AI_GATEWAY_API_KEY غير موجود في إعدادات البيئة."
    )

llm = ChatOpenAI(
    model="nvidia/nemotron-3.5-lightning-free",
    temperature=0.3,
    api_key=api_key,
    base_url="https://ai-gateway.vercel.sh/v1",
)

# تعليمات المساعد ومعلومات الشركة
system_prompt = (
    "أنت مساعد خدمة عملاء ذكي ومؤدب وودود.\n"
    "أجب باللغة العربية بأسلوب احترافي ومختصر.\n"
    "اعتمد على معلومات الشركة التالية عند الإجابة عن أسئلة الشركة.\n"
    "لا تخترع أسعارًا أو سياسات أو معلومات غير مؤكدة.\n"
    "إذا لم تجد المعلومة، وضح للعميل أنك تحتاج إلى التأكد منها.\n\n"
    "معلومات الشركة:\n"
    + (company_knowledge or "لم تُضف معلومات الشركة بعد.")
)


def generate_ai_response(
    user_phone: str,
    user_message: str,
    image_bytes: bytes = None
) -> str:
    history = get_session_history(user_phone)

    if image_bytes:
        base64_image = base64.b64encode(image_bytes).decode("utf-8")

        image_message = HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": user_message or
                    "صف الصورة وساعدني بشأنها."
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}"
                    }
                }
            ]
        )

        messages = (
            [SystemMessage(content=system_prompt)]
            + history.messages
            + [image_message]
        )
    else:
        messages = (
            [SystemMessage(content=system_prompt)]
            + history.messages
            + [HumanMessage(content=user_message)]
        )

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
    """تحويل الصوت إلى نص عبر OpenAI Whisper عند توفر الرصيد والمفتاح."""

    openai_api_key = os.getenv("OPENAI_API_KEY")

    if not openai_api_key:
        raise RuntimeError(
            "تحويل الصوت يحتاج إلى OPENAI_API_KEY صالح."
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
