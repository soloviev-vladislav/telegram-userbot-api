import os
import requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from contextlib import asynccontextmanager
import uvicorn

# ====================== НАСТРОЙКИ ======================
API_ID = 31407487
API_HASH = "0b82a91fb5c797a2bf713ad3d46a9c20"

SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")   # ← сюда вставишь строку после первого входа
WEBHOOK_URL    = os.getenv("WEBHOOK_URL")               # ← твой n8n webhook
BOT_PORT       = int(os.getenv("BOT_PORT", "8000"))

# ====================== TELETHON (всегда StringSession) ======================
if SESSION_STRING and SESSION_STRING.strip() and SESSION_STRING != "None":
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
else:
    client = TelegramClient(StringSession(), API_ID, API_HASH)   # чистая строка в памяти

# ====================== FASTAPI ======================
class SendMessageRequest(BaseModel):
    chat_id: str | int
    text: str

@asynccontextmanager
async def lifespan(app: FastAPI):
    await client.connect()

    if not await client.is_user_authorized():
        print("\nТребуется первая авторизация")
        phone = input("Номер телефона (с +): ")
        await client.send_code_request(phone)
        code = input("Код из Telegram: ")
        try:
            await client.sign_in(phone, code)
        except Exception:
            password = input("2FA пароль: ")
            await client.sign_in(password=password)

        session_str = client.session.save()          # ← теперь всегда реальная строка!
        print("\n" + "="*70)
        print("АВТОРИЗАЦИЯ УСПЕШНА! Скопируй строку ниже и сохрани навсегда:")
        print(session_str)
        print("="*70 + "\n")

    await client.start()
    print("Telegram-клиент запущен и готов")
    yield
    await client.disconnect()
    print("Telegram-клиент остановлен")

app = FastAPI(title="Telegram Userbot API", lifespan=lifespan)

# ====================== API отправки ======================
@app.post("/send_message")
async def send_message(request: SendMessageRequest):
    try:
        await client.send_message(request.chat_id, request.text)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(500, detail=str(e))

# ====================== Входящие сообщения → n8n ======================
@client.on(events.NewMessage(incoming=True))
async def incoming_handler(event):
    try:
        # В Telethon 1.36+ правильный способ определить исходящее сообщение:
        if getattr(event, "outgoing", False):           # ← безопасно и работает всегда
            return

        me = await client.get_me()
        if event.sender_id == me.id:                    # свои сообщения тоже игнорим
            return

        if not WEBHOOK_URL:
            return

        payload = {
            "sender_id": event.sender_id,
            "chat_id"   : event.chat_id,
            "message_id": event.id,
            "text"      : event.text or "",
            "date"      : event.date.isoformat(),
        }

        requests.post(WEBHOOK_URL, json=payload, timeout=10)
        print(f"Вебхук отправлен: {event.sender_id} → {event.text[:40]}")

    except Exception as e:
        print("Ошибка в обработчике сообщений:", e)    # бот НЕ упадёт

# ====================== ЗАПУСК ======================
if __name__ == "__main__":
    uvicorn.run("telegram_bot:app", host="0.0.0.0", port=BOT_PORT)