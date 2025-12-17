# telegram_bot.py — Мультиаккаунт + экспорт участников + поиск по номеру
import os
import asyncio
import requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, PhoneNumberInvalidError
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from contextlib import asynccontextmanager
import uvicorn
from typing import List, Dict, Optional
import time

API_ID = 31407487
API_HASH = "0b82a91fb5c797a2bf713ad3d46a9c20"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
RESULTS_WEBHOOK_URL = os.getenv("RESULTS_WEBHOOK_URL", "")

# Хранилища
ACTIVE_CLIENTS = {}
PENDING_AUTH = {}
SEARCH_TASKS = {}  # Для отслеживания задач поиска

# ==================== Модели ====================
class SendMessageReq(BaseModel):
    account: str
    chat_id: str | int
    text: str

class AddAccountReq(BaseModel):
    name: str
    session_string: str

class RemoveAccountReq(BaseModel):
    name: str

class AuthStartReq(BaseModel):
    phone: str

class AuthCodeReq(BaseModel):
    phone: str
    code: str
    password: str | None = None

class ExportMembersReq(BaseModel):
    account: str
    group: str | int

class SearchByPhoneReq(BaseModel):
    account: str  # какой аккаунт использовать для поиска
    phones: List[str]  # список номеров телефонов
    webhook_url: Optional[str] = None  # опциональный вебхук для результатов
    task_id: Optional[str] = None  # ID задачи для отслеживания
    include_username: bool = True  # включать username
    include_name: bool = True  # включать имя
    include_photo: bool = False  # включать информацию о фото

class SearchStatusReq(BaseModel):
    task_id: str

class SearchResult(BaseModel):
    phone: str
    telegram_id: Optional[int] = None
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    photo: Optional[bool] = None
    found: bool = False
    error: Optional[str] = None
    account_used: str

# ==================== Обработчик входящих ====================
async def incoming_handler(event):
    if event.is_outgoing:
        return

    from_account = "unknown"
    for name, cl in ACTIVE_CLIENTS.items():
        if cl.session == event.client.session:
            from_account = name
            break

    payload = {
        "from_account": from_account,
        "sender_id": event.sender_id,
        "chat_id": event.chat_id,
        "message_id": event.id,
        "text": event.text or "",
        "date": event.date.isoformat() if event.date else None,
    }

    if WEBHOOK_URL:
        try:
            requests.post(WEBHOOK_URL, json=payload, timeout=12)
        except:
            pass

# ==================== ПОИСК ПО НОМЕРАМ ====================
async def search_contacts_by_phones_task(
    account_name: str,
    phones: List[str],
    task_id: str,
    webhook_url: Optional[str] = None,
    include_username: bool = True,
    include_name: bool = True,
    include_photo: bool = False
):
    """Фоновая задача поиска контактов по номерам"""
    client = ACTIVE_CLIENTS.get(account_name)
    if not client:
        SEARCH_TASKS[task_id] = {
            "status": "error",
            "error": f"Аккаунт {account_name} не найден",
            "results": []
        }
        return
    
    SEARCH_TASKS[task_id] = {
        "status": "processing",
        "progress": 0,
        "total": len(phones),
        "processed": 0,
        "results": [],
        "started_at": time.time()
    }
    
    results = []
    
    for i, phone in enumerate(phones):
        try:
            # Очищаем номер от лишних символов
            clean_phone = ''.join(filter(str.isdigit, str(phone)))
            if not clean_phone:
                result = SearchResult(
                    phone=phone,
                    found=False,
                    error="Invalid phone number",
                    account_used=account_name
                )
                results.append(result.dict())
                continue
            
            # Ищем контакт по номеру
            try:
                contact = await client.get_entity(clean_phone)
                
                # Получаем фото если нужно
                has_photo = False
                if include_photo and contact.photo:
                    try:
                        photo_info = await client.get_profile_photos(contact.id, limit=1)
                        has_photo = len(photo_info) > 0
                    except:
                        has_photo = False
                
                result = SearchResult(
                    phone=phone,
                    telegram_id=contact.id,
                    username=contact.username if include_username else None,
                    first_name=contact.first_name if include_name else None,
                    last_name=contact.last_name if include_name else None,
                    photo=has_photo if include_photo else None,
                    found=True,
                    account_used=account_name
                )
                
            except ValueError:
                # Контакт не найден
                result = SearchResult(
                    phone=phone,
                    found=False,
                    account_used=account_name
                )
            
            results.append(result.dict())
            
            # Обновляем прогресс в задаче
            SEARCH_TASKS[task_id]["processed"] = i + 1
            SEARCH_TASKS[task_id]["progress"] = (i + 1) / len(phones) * 100
            SEARCH_TASKS[task_id]["results"] = results
            
            # Пауза между запросами чтобы избежать лимитов
            await asyncio.sleep(0.5)
            
        except FloodWaitError as e:
            # Ожидание при флуде
            wait_time = e.seconds
            result = SearchResult(
                phone=phone,
                found=False,
                error=f"Flood wait: {wait_time} seconds",
                account_used=account_name
            )
            results.append(result.dict())
            SEARCH_TASKS[task_id]["results"] = results
            await asyncio.sleep(wait_time)
            
        except Exception as e:
            # Обработка других ошибок
            result = SearchResult(
                phone=phone,
                found=False,
                error=str(e),
                account_used=account_name
            )
            results.append(result.dict())
            SEARCH_TASKS[task_id]["results"] = results
    
    # Обновляем статус задачи
    SEARCH_TASKS[task_id].update({
        "status": "completed",
        "completed_at": time.time(),
        "results": results,
        "total_found": sum(1 for r in results if r.get("found", False))
    })
    
    # Отправляем результаты на вебхук если указан
    if webhook_url:
        try:
            final_payload = {
                "task_id": task_id,
                "status": "completed",
                "account_used": account_name,
                "total_phones": len(phones),
                "total_found": SEARCH_TASKS[task_id]["total_found"],
                "results": results
            }
            requests.post(webhook_url, json=final_payload, timeout=30)
        except Exception as e:
            print(f"Ошибка отправки вебхука: {e}")

# ==================== Lifespan ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Telegram Multi Gateway запущен")
    yield
    for client in ACTIVE_CLIENTS.values():
        await client.disconnect()
    print("Все аккаунты отключены")

app = FastAPI(title="Telegram Multi Account Gateway", lifespan=lifespan)

# ==================== Добавить аккаунт ====================
@app.post("/accounts/add")
async def add_account(req: AddAccountReq):
    if req.name in ACTIVE_CLIENTS:
        raise HTTPException(400, detail=f"Аккаунт {req.name} уже существует")

    client = TelegramClient(StringSession(req.session_string), API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        raise HTTPException(400, detail="Сессия недействительна или просрочена")

    await client.start()

    try:
        dialogs = await client.get_dialogs(limit=50)
        print(f"Прогрет кэш для {req.name}: {len(dialogs)} чатов")
    except Exception as e:
        print(f"Не удалось прогреть кэш для {req.name}: {e}")

    ACTIVE_CLIENTS[req.name] = client
    client.add_event_handler(incoming_handler, events.NewMessage(incoming=True))

    return {
        "status": "added",
        "account": req.name,
        "total_accounts": len(ACTIVE_CLIENTS),
        "cache_warmed": True
    }

# ==================== Удалить аккаунт ====================
@app.delete("/accounts/{name}")
async def remove_account(name: str):
    client = ACTIVE_CLIENTS.pop(name, None)
    if client:
        await client.disconnect()
        return {"status": "removed", "account": name}
    raise HTTPException(404, detail="Аккаунт не найден")

# ==================== Список аккаунтов ====================
@app.get("/accounts")
def list_accounts():
    return {"active_accounts": list(ACTIVE_CLIENTS.keys())}

# ==================== Отправить сообщение ====================
@app.post("/send")
async def send_message(req: SendMessageReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        await client.send_message(req.chat_id, req.text)
        return {"status": "sent", "from": req.account, "to": req.chat_id}
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка отправки: {str(e)}")

# ==================== Экспорт участников группы ====================
@app.post("/export_members")
async def export_members(req: ExportMembersReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        group = await client.get_entity(req.group)
        participants = await client.get_participants(group, aggressive=True)

        members = [
            {
                "id": p.id,
                "username": p.username,
                "first_name": p.first_name,
                "last_name": p.last_name,
                "phone": p.phone if p.phone else None,
                "is_admin": p.admin_rights is not None,
                "is_bot": p.bot,
            }
            for p in participants
        ]

        return {
            "status": "exported",
            "group": req.group,
            "total_members": len(members),
            "members": members
        }
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка экспорта: {str(e)}")

# ==================== ПОИСК ПО НОМЕРУ ТЕЛЕФОНА ====================
@app.post("/search/by_phone")
async def search_by_phone(req: SearchByPhoneReq, background_tasks: BackgroundTasks):
    """Запуск поиска контактов по номерам телефонов"""
    if req.account not in ACTIVE_CLIENTS:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")
    
    # Генерируем ID задачи если не указан
    task_id = req.task_id or f"search_{int(time.time())}_{hash(tuple(req.phones)) % 10000}"
    
    # Запускаем фоновую задачу
    background_tasks.add_task(
        search_contacts_by_phones_task,
        account_name=req.account,
        phones=req.phones,
        task_id=task_id,
        webhook_url=req.webhook_url or RESULTS_WEBHOOK_URL,
        include_username=req.include_username,
        include_name=req.include_name,
        include_photo=req.include_photo
    )
    
    return {
        "status": "search_started",
        "task_id": task_id,
        "account": req.account,
        "total_phones": len(req.phones),
        "webhook_url": req.webhook_url or RESULTS_WEBHOOK_URL,
        "check_status_url": f"/search/status/{task_id}"
    }

@app.get("/search/status/{task_id}")
async def get_search_status(task_id: str):
    """Получить статус задачи поиска"""
    task = SEARCH_TASKS.get(task_id)
    if not task:
        raise HTTPException(404, detail="Задача не найдена")
    
    return {
        "task_id": task_id,
        "status": task.get("status", "unknown"),
        "progress": task.get("progress", 0),
        "processed": task.get("processed", 0),
        "total": task.get("total", 0),
        "total_found": task.get("total_found", 0),
        "started_at": task.get("started_at"),
        "completed_at": task.get("completed_at"),
        "results": task.get("results", []) if task.get("status") == "completed" else []
    }

@app.get("/search/results/{task_id}")
async def get_search_results(task_id: str):
    """Получить результаты поиска (только если задача завершена)"""
    task = SEARCH_TASKS.get(task_id)
    if not task:
        raise HTTPException(404, detail="Задача не найдена")
    
    if task.get("status") != "completed":
        raise HTTPException(400, detail="Задача еще не завершена")
    
    return {
        "task_id": task_id,
        "status": "completed",
        "total_processed": task.get("processed", 0),
        "total_found": task.get("total_found", 0),
        "results": task.get("results", [])
    }

# ==================== Авторизация по API ====================
@app.post("/auth/start")
async def auth_start(req: AuthStartReq):
    if req.phone in PENDING_AUTH:
        raise HTTPException(400, "Авторизация уже идёт")
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
        return {"status": "success", "session_string": session_str}
    except Exception as e:
        raise HTTPException(400, detail=str(e))

# ==================== Запуск ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("telegram_bot:app", host="0.0.0.0", port=port, reload=False)
