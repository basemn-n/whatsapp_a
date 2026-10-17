import os
import requests
import tempfile
from fastapi import FastAPI, Request, Response, HTTPException
from dotenv import load_dotenv
from bot_logic import generate_ai_response, transcribe_audio

load_dotenv()

app = FastAPI()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

def send_whatsapp_message(to_phone: str, message_text: str):
    """إرسال رد نصي إلى العميل عبر Meta API"""
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": message_text}
    }
    requests.post(url, json=data, headers=headers)

def download_whatsapp_media(media_id: str) -> bytes:
    """تحميل الوسائط (صورة أو صوت) من واتساب"""
    media_url_endpoint = f"https://graph.facebook.com/v19.0/{media_id}"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    res = requests.get(media_url_endpoint, headers=headers).json()
    media_url = res.get("url")
    
    if not media_url:
        return None
    
    media_res = requests.get(media_url, headers=headers)
    return media_res.content

@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")

@app.post("/webhook")
async def receive_webhook(request: Request):
    body = await request.json()
    
    try:
        entries = body.get("entry", [])
        for entry in entries:
            changes = entry.get("changes", [])
            for change in changes:
                value = change.get("value", {})
                messages = value.get("messages", [])
                
                if messages:
                    message = messages[0]
                    from_number = message["from"]
                    msg_type = message.get("type")
                    
                    user_text = ""
                    image_bytes = None
                    
                    if msg_type == "text":
                        user_text = message["text"]["body"]
                        ai_reply = generate_ai_response(from_number, user_text)
                        send_whatsapp_message(from_number, ai_reply)
                        
                    elif msg_type == "image":
                        image_id = message["image"]["id"]
                        user_text = message["image"].get("caption", "صورة مرسلة من العميل")
                        image_bytes = download_whatsapp_media(image_id)
                        ai_reply = generate_ai_response(from_number, user_text, image_bytes)
                        send_whatsapp_message(from_number, ai_reply)
                        
                    elif msg_type == "audio" or msg_type == "voice":
                        audio_id = message.get("audio", {}).get("id") or message.get("voice", {}).get("id")
                        audio_bytes = download_whatsapp_media(audio_id)
                        
                        if audio_bytes:
                            with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as temp_audio:
                                temp_audio.write(audio_bytes)
                                temp_audio_path = temp_audio.name
                            
                            try:
                                user_text = transcribe_audio(temp_audio_path)
                            finally:
                                if os.path.exists(temp_audio_path):
                                    os.remove(temp_audio_path)
                                    
                            ai_reply = generate_ai_response(from_number, user_text)
                            send_whatsapp_message(from_number, ai_reply)
                            
    except Exception as e:
        print(f"Error processing webhook: {e}")
        
    return {"status": "ok"}

from pydantic import BaseModel

class TestAIRequest(BaseModel):
    message: str

@app.post("/test-ai")
async def test_ai(request: TestAIRequest):
    try:
        reply = generate_ai_response(
            user_phone="test-session",
            user_message=request.message
        )
        return {"success": True, "reply": reply}
    except Exception as e:
        print(f"AI test error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"فشل اختبار الذكاء الاصطناعي: {str(e)}"
        )
