import os
import base64

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_community.chat_message_histories import ChatMessageHistory

load_dotenv()

company_knowledge = ""

if os.path.exists("company_knowledge.txt"):
    with open("company_knowledge.txt", "r", encoding="utf-8") as f:
        company_knowledge = f.read()

store = {}


def get_session_history(session_id: str) -> ChatMessageHistory:
    if session_id not in store:
        store[session_id] = ChatMessageHistory()
    return store[session_id]


api_key = os.getenv("AI_GATEWAY_API_KEY")

if not api_key:
    raise RuntimeError(
        "AI_GATEWAY_API_KEY غير موجود في إعدادات Render."
    )

llm = ChatOpenAI(
    model="openai/gpt-4o-mini",
    temperature=0.3,
    api_key=api_key,
    base_url="https://ai-gateway.vercel.sh/v1",
)

system_prompt = (
    "أنت مساعد خدمة عملاء ذكي ومؤدب وودود.\n"
    "أجب باللغة العربية باختصار واحترافية.\n"
    "اعتمد على معلومات الشركة أدناه، ولا تخترع الأسعار أو السياسات.\n"
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

    messages = [SystemMessage(content=system_prompt)]
    messages.extend(history.messages)

    if image_bytes:
        base64_image = base64.b64encode(image_bytes).decode("utf-8")
        user_content = [
            {
                "type": "text",
                "text": user_message or "صف الصورة وساعد العميل بشأنها."
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

    history.add_user_message(user_message or "[صورة مرفقة]")
    history.add_ai_message(answer)

    return answer


def transcribe_audio(audio_file_path: str) -> str:
    raise RuntimeError(
        "تحويل الصوت غير مفعّل في هذا الإصدار؛ "
        "سنفعّله لاحقًا إذا احتجت إليه."
    )
