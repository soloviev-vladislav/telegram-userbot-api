# telegram_bot.py — Мультиаккаунт БЕЗ перезапуска сервера
import os
import requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from contextlib import asynccontextmanager
import uvicorn

API_ID = 31407487
API_HASH = "0b82a91fb5c797a2bf713ad3d46a9c20"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # можно оставить пустым, если не нужен

# Глобальный словарь: имя_аккаунта → TelegramClient
ACTIVE_CLIENTS = {}

# ==================== Pydantic модели ====================
class SendMessageReq(BaseModel):
    account: str        # например: "main", "shop", "personal"
    chat_id: str | int
    text: str

class AddAccountReq(BaseModel):
    name: str           # как будешь называть аккаунт: main, shop2 и т.д.
    session_string: str     # полная session string из авторизации

class AuthStartReq(BaseModel):
    phone: str

class AuthCodeReq(BaseModel):
    phone: str
    code: str
    password: str | None = None

# Временное хранение клиентов в процессе авторизации
PENDING_AUTH = {}

# ==================== Lifespan ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Сервер запущен. Готов принимать аккаунты по API.")
    yield
    # При выключении — отключаем все клиенты
    for client in ACTIVE_CLIENTS.values():
        await client.disconnect()

app = FastAPI(title="Telegram Multi Account Gateway", lifespan=lifespan)

# ==================== API: добавить аккаунт ====================
@app.post("/accounts/add")
async def add_account(req: AddAccountReq):
    if req.name in ACTIVE_CLIENTS:
        raise HTTPException(400, detail=f"Аккаунт {req.name} уже существует")

    try:
        client = TelegramClient(StringSession(req.session_string), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise HTTPException(400, detail="Сессия недействительна или просрочена")

        await client.start()
        ACTIVE_CLIENTS[req.name] = client

        # Подписываемся на входящие сообщения
        client.add_event_handler(incoming_handler, events.NewMessage(incoming=True))

        return {"status": "added", "account": req.name, "total": len(ACTIVE_CLIENTS)}
    except Exception as e:
        raise HTTPException(500, detail=str(e))

# ==================== API: удалить аккаунт ====================
@app.delete("/accounts/{name}")
async def remove_account(name: str):
    client = ACTIVE_CLIENTS.pop(name, None)
    if client:
        await client.disconnect()
        return {"status": "removed", "account": name}
    raise HTTPException(404, detail="Аккаунт не найден")

# ==================== API: список аккаунтов ====================
@app.get("/accounts")
async def list_accounts():
    return {"accounts": list(ACTIVE_CLIENTS.keys())}

# ==================== API: отправить сообщение ====================
@app.post("/send")
async def send_message(req: SendMessageReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}. Доступные: {list(ACTIVE_CLIENTS.keys())}")
    await client.send_message(req.chat_id, req.text)
    return {"status": "sent", "from": req.account}

# ==================== Авторизация по API (опционально) ====================
@app.post("/auth/start")
async def auth_start(req: AuthStartReq):
    if req.phone in PENDING_AUTH:
        raise HTTPException(400, "Уже в процессе")
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    await client.send_code_request(req.phone)
    PENDING_AUTH[req.phone] = client
    return {"status": "code_sent"}

@app.post("/auth/complete")
async def auth_complete(req: AuthCodeReq):
    client = PENDING_AUTH.get(req.phone)
    if not client:
        raise HTTPException(400, "Нет активной авторизации")
    try:
        await client.sign_in(req.phone, req.code, password=req.password)
        session_str = client.session.save()
        del PENDING_AUTH[req.phone]
        return {"session_string": session_str, "hint": "Используй /accounts/add"}
    except Exception as e:
        raise HTTPException(400, str(e))

# ==================== Входящие сообщения → один общий вебхук ====================
async def incoming_handler(event):
    if event.is_outgoing:
        return

    # Определяем, от какого аккаунта пришло сообщение
    from_account = "unknown"
    for name, cl in ACTIVE_CLIENTS.items():
        if cl.session == event.client.session:
            from_account = name
            break

    payload = {
        "from_account": from_account,
        "sender_id": event.sender_id,
        "chat_id": event.chat_id,
        "text": event.text or "",
        "date": event.date.isoformat(),
    }

    if WEBHOOK_URL:
        try:
            requests.post(WEBHOOK_URL, json=payload, timeout=10)
        except:
            pass

# ==================== Запуск =================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("telegram_bot:app", host="0.0.0.0", port=port, reload=False)
