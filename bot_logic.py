import os
import base64
from dotenv import load_dotenv
from langchain_community.document_loaders import TextLoader
from langchain_text_splitters import CharacterTextSplitter
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_community.vectorstores import Chroma
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.chains import create_history_aware_retriever, create_retrieval_chain
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.messages import HumanMessage
from openai import OpenAI

load_dotenv()
client = OpenAI()

# 1. إعداد قاعدة المعرفة (Vector Database)
if os.path.exists("company_knowledge.txt"):
    loader = TextLoader("company_knowledge.txt", encoding="utf-8")
    documents = loader.load()
    text_splitter = CharacterTextSplitter(chunk_size=500, chunk_overlap=50)
    docs = text_splitter.split_documents(documents)
    embeddings = OpenAIEmbeddings()
    vectorstore = Chroma.from_documents(docs, embeddings)
    retriever = vectorstore.as_retriever(search_kwargs={"k": 2})
else:
    retriever = None

# 2. إعداد الذاكرة للمحادثات (Memory Store)
store = {}

def get_session_history(session_id: str) -> BaseChatMessageHistory:
    if session_id not in store:
        store[session_id] = ChatMessageHistory()
    return store[session_id]

# 3. إعداد النموذج والموجهات (LLM & Prompts)
llm = ChatOpenAI(model="gpt-4o", temperature=0.3)

system_prompt = (
    "أنت مساعد خدمة عملاء ذكي ومؤدب وودود.\n"
    "أجب على أسئلة العميل بناءً على المعلومات المرفقة في السياق فقط إن وجدت.\n"
    "إذا لم تكن المعلومة موجودة في السياق، أجب بذكاء واحترافية بناءً على قدرات الذكاء الاصطناعي مع الحفاظ على هوية خدمة العملاء.\n"
    "تحدث دائماً باللغة العربية بأسلوب احترافي ومختصر.\n\n"
    "{context}"
)

def generate_ai_response(user_phone: str, user_message: str, image_bytes: bytes = None) -> str:
    history = get_session_history(user_phone)
    
    # إذا كانت هناك صورة مرفقة مع الرسالة
    if image_bytes:
        base64_image = base64.b64encode(image_bytes).decode("utf-8")
        image_message = HumanMessage(
            content=[
                {"type": "text", "text": user_message if user_message else "ماذا ترى في هذه الصورة وكيف يمكنني مساعدتك بخصوصها؟"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
            ]
        )
        messages = history.messages + [image_message]
        response = llm.invoke(messages)
        answer = response.content
    else:
        if retriever:
            contextualize_q_system_prompt = (
                "Given a chat history and the latest user question "
                "which might reference context in the chat history, "
                "formulate a standalone question which can be understood "
                "without the chat history."
            )
            contextualize_q_prompt = ChatPromptTemplate.from_messages([
                ("system", contextualize_q_system_prompt),
                MessagesPlaceholder("chat_history"),
                ("human", "{input}"),
            ])
            history_aware_retriever = create_history_aware_retriever(
                llm, retriever, contextualize_q_prompt
            )
            qa_prompt = ChatPromptTemplate.from_messages([
                ("system", system_prompt),
                MessagesPlaceholder("chat_history"),
                ("human", "{input}"),
            ])
            question_answer_chain = create_stuff_documents_chain(llm, qa_prompt)
            rag_chain = create_retrieval_chain(history_aware_retriever, question_answer_chain)
            
            response = rag_chain.invoke(
                {"input": user_message, "chat_history": history.messages}
            )
            answer = response["answer"]
        else:
            messages = history.messages + [HumanMessage(content=user_message)]
            response = llm.invoke(messages)
            answer = response.content

    history.add_user_message(user_message if user_message else "[صورة مرفقة]")
    history.add_ai_message(answer)
    
    return answer

def transcribe_audio(audio_file_path: str) -> str:
    """تحويل الرسالة الصوتية إلى نص باستخدام Whisper API"""
    with open(audio_file_path, "rb") as audio_file:
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="ar"
        )
    return transcript.text
