# telegram_bot_pyrogram.py — Pyrogram мультиаккаунт с поиском ID через вебхуки
import os
import asyncio
import json
import time
import hashlib
from datetime import datetime
from typing import List, Optional, Dict, Any
from pyrogram import Client
from pyrogram.types import InputPhoneContact
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from contextlib import asynccontextmanager
import uvicorn
import aiohttp
import logging

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

API_ID = int(os.getenv("API_ID", 30407407))
API_HASH = os.getenv("API_HASH", "0b81a91fb5c797a2bf713ad3d46a9c20")
DEFAULT_DELAY = float(os.getenv("DELAY_BETWEEN_REQUESTS", 1.5))

# Хранилище аккаунтов: имя → client
ACTIVE_CLIENTS = {}
SEARCH_TASKS = {}  # task_id → task_info

# ==================== Модели Pydantic ====================
class SendMessageReq(BaseModel):
    account: str
    chat_id: str | int
    text: str

class AddAccountReq(BaseModel):
    name: str
    session_string: str

class RemoveAccountReq(BaseModel):
    name: str

class ExportMembersReq(BaseModel):
    account: str
    group: str | int

class SearchByPhoneReq(BaseModel):
    account: str
    phones: List[str]
    webhook_url: str  # Обязательный вебхук для результатов
    task_id: Optional[str] = None
    delay_between: float = DEFAULT_DELAY

class CheckAccountReq(BaseModel):
    account: str

# ==================== Вспомогательные функции ====================
def format_phone_number(phone: str) -> str:
    """Форматирует номер телефона"""
    phone = str(phone).strip()
    
    # Удаляем все нецифровые символы
    digits = ''.join(filter(str.isdigit, phone))
    
    if not digits:
        return phone
    
    if len(digits) == 10 and digits[0] == '9':
        return '+7' + digits
    elif len(digits) == 11 and digits[0] == '8':
        return '+7' + digits[1:]
    elif len(digits) == 11 and digits[0] == '7':
        return '+' + digits
    elif phone.startswith('+'):
        return phone
    else:
        return '+' + digits

async def send_webhook(url: str, data: Dict[str, Any]):
    """Отправляет данные на вебхук"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data, timeout=30) as response:
                if response.status != 200:
                    logger.error(f"Webhook error: {response.status}")
                else:
                    logger.info(f"Webhook sent to {url}")
    except Exception as e:
        logger.error(f"Failed to send webhook: {e}")

async def search_single_phone(client: Client, phone: str) -> Dict[str, Any]:
    """
    Ищет Telegram ID для одного номера
    Возвращает dict с результатом
    """
    formatted_phone = format_phone_number(phone)
    timestamp = datetime.now().isoformat()
    
    try:
        # Генерируем уникальное имя для временного контакта
        temp_name = f"search_{int(time.time())}_{hashlib.md5(phone.encode()).hexdigest()[:8]}"
        
        logger.info(f"Searching for phone: {phone} (formatted: {formatted_phone})")
        
        # Импортируем контакт
        await client.import_contacts([
            InputPhoneContact(
                phone=formatted_phone,
                first_name=temp_name,
                last_name=""
            )
        ])
        
        # Ждем синхронизацию
        await asyncio.sleep(0.5)
        
        # Получаем контакты
        contacts = await client.get_contacts()
        
        # Ищем наш временный контакт
        found_contact = None
        for contact in contacts:
            if contact.first_name == temp_name:
                found_contact = contact
                break
        
        # Удаляем временный контакт
        if found_contact:
            await client.delete_contacts([found_contact.id])
            
            result = {
                "phone": phone,
                "formatted_phone": formatted_phone,
                "telegram_id": found_contact.id,
                "username": found_contact.username,
                "first_name": found_contact.first_name,
                "last_name": found_contact.last_name,
                "found": True,
                "status": "found",
                "timestamp": timestamp
            }
            logger.info(f"Found ID {found_contact.id} for {phone}")
            return result
        else:
            result = {
                "phone": phone,
                "formatted_phone": formatted_phone,
                "telegram_id": None,
                "found": False,
                "status": "not_found",
                "timestamp": timestamp
            }
            logger.info(f"Not found for {phone}")
            return result
            
    except Exception as e:
        logger.error(f"Error searching {phone}: {str(e)}")
        return {
            "phone": phone,
            "formatted_phone": formatted_phone,
            "telegram_id": None,
            "found": False,
            "status": "error",
            "error": str(e),
            "timestamp": timestamp
        }

async def search_phones_task(
    account_name: str,
    phones: List[str],
    task_id: str,
    webhook_url: str,
    delay_between: float = DEFAULT_DELAY
):
    """
    Фоновая задача поиска ID для списка номеров
    Отправляет промежуточные и финальные результаты на вебхук
    """
    client = ACTIVE_CLIENTS.get(account_name)
    
    if not client:
        error_data = {
            "task_id": task_id,
            "status": "error",
            "error": f"Account {account_name} not found",
            "timestamp": datetime.now().isoformat()
        }
        await send_webhook(webhook_url, error_data)
        return
    
    # Инициализируем задачу
    SEARCH_TASKS[task_id] = {
        "status": "processing",
        "total": len(phones),
        "processed": 0,
        "found": 0,
        "not_found": 0,
        "errors": 0,
        "started_at": datetime.now().isoformat(),
        "results": []
    }
    
    # Отправляем стартовый вебхук
    start_data = {
        "task_id": task_id,
        "status": "started",
        "account": account_name,
        "total_phones": len(phones),
        "timestamp": datetime.now().isoformat()
    }
    await send_webhook(webhook_url, start_data)
    
    results = []
    
    # Обрабатываем каждый номер
    for i, phone in enumerate(phones):
        try:
            # Поиск ID
            result = await search_single_phone(client, phone)
            result["account_used"] = account_name
            results.append(result)
            
            # Обновляем статистику
            if result["status"] == "found":
                SEARCH_TASKS[task_id]["found"] += 1
            elif result["status"] == "not_found":
                SEARCH_TASKS[task_id]["not_found"] += 1
            else:
                SEARCH_TASKS[task_id]["errors"] += 1
            
            SEARCH_TASKS[task_id]["processed"] = i + 1
            SEARCH_TASKS[task_id]["results"] = results
            
            # Отправляем промежуточный результат
            if (i + 1) % 5 == 0 or i + 1 == len(phones):  # Каждые 5 номеров или в конце
                progress_data = {
                    "task_id": task_id,
                    "status": "progress",
                    "account": account_name,
                    "processed": i + 1,
                    "total": len(phones),
                    "found": SEARCH_TASKS[task_id]["found"],
                    "not_found": SEARCH_TASKS[task_id]["not_found"],
                    "errors": SEARCH_TASKS[task_id]["errors"],
                    "progress_percent": round((i + 1) / len(phones) * 100, 1),
                    "timestamp": datetime.now().isoformat()
                }
                await send_webhook(webhook_url, progress_data)
            
            # Задержка между запросами
            if i + 1 < len(phones):
                await asyncio.sleep(delay_between)
                
        except Exception as e:
            logger.error(f"Task error for phone {phone}: {e}")
            error_result = {
                "phone": phone,
                "formatted_phone": format_phone_number(phone),
                "telegram_id": None,
                "found": False,
                "status": "error",
                "error": str(e),
                "account_used": account_name,
                "timestamp": datetime.now().isoformat()
            }
            results.append(error_result)
            SEARCH_TASKS[task_id]["errors"] += 1
    
    # Финальный результат
    SEARCH_TASKS[task_id]["status"] = "completed"
    SEARCH_TASKS[task_id]["completed_at"] = datetime.now().isoformat()
    
    final_data = {
        "task_id": task_id,
        "status": "completed",
        "account": account_name,
        "total_phones": len(phones),
        "processed": len(phones),
        "found": SEARCH_TASKS[task_id]["found"],
        "not_found": SEARCH_TASKS[task_id]["not_found"],
        "errors": SEARCH_TASKS[task_id]["errors"],
        "results": results,
        "started_at": SEARCH_TASKS[task_id]["started_at"],
        "completed_at": SEARCH_TASKS[task_id]["completed_at"],
        "timestamp": datetime.now().isoformat()
    }
    
    await send_webhook(webhook_url, final_data)
    logger.info(f"Task {task_id} completed. Found: {SEARCH_TASKS[task_id]['found']}")

# ==================== Lifespan ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Pyrogram Multi-Account Gateway starting...")
    yield
    logger.info("Shutting down...")
    # Останавливаем всех клиентов
    for name, client in ACTIVE_CLIENTS.items():
        try:
            await client.stop()
            logger.info(f"Account {name} stopped")
        except Exception as e:
            logger.error(f"Error stopping account {name}: {e}")

app = FastAPI(
    title="Pyrogram Telegram ID Finder",
    description="Мультиаккаунтный поиск Telegram ID по номеру телефона",
    version="1.0.0"
)

# ==================== API Endpoints ====================
@app.get("/")
async def root():
    return {
        "service": "Pyrogram Telegram ID Finder",
        "version": "1.0.0",
        "active_accounts": len(ACTIVE_CLIENTS),
        "endpoints": {
            "add_account": "POST /accounts/add",
            "list_accounts": "GET /accounts",
            "remove_account": "DELETE /accounts/{name}",
            "check_account": "GET /accounts/{name}/check",
            "search_by_phone": "POST /search/by_phone",
            "task_status": "GET /search/status/{task_id}"
        }
    }

@app.post("/accounts/add")
async def add_account(req: AddAccountReq):
    """Добавить аккаунт по строке сессии"""
    if req.name in ACTIVE_CLIENTS:
        raise HTTPException(400, detail=f"Account {req.name} already exists")
    
    try:
        # Создаем клиент Pyrogram
        client = Client(
            name=req.name,
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=req.session_string,
            in_memory=True
        )
        
        # Запускаем клиент
        await client.start()
        
        # Проверяем авторизацию
        me = await client.get_me()
        
        ACTIVE_CLIENTS[req.name] = client
        
        logger.info(f"Account {req.name} added successfully. User: @{me.username or me.id}")
        
        return {
            "status": "success",
            "account": req.name,
            "user_id": me.id,
            "username": me.username,
            "phone": me.phone_number,
            "first_name": me.first_name,
            "active_accounts": len(ACTIVE_CLIENTS)
        }
        
    except Exception as e:
        logger.error(f"Error adding account {req.name}: {e}")
        raise HTTPException(500, detail=f"Failed to add account: {str(e)}")

@app.delete("/accounts/{name}")
async def remove_account(name: str):
    """Удалить аккаунт"""
    client = ACTIVE_CLIENTS.pop(name, None)
    if client:
        try:
            await client.stop()
            logger.info(f"Account {name} removed")
            return {"status": "removed", "account": name}
        except Exception as e:
            raise HTTPException(500, detail=f"Error stopping account: {str(e)}")
    else:
        raise HTTPException(404, detail="Account not found")

@app.get("/accounts")
async def list_accounts():
    """Список активных аккаунтов"""
    accounts = []
    for name, client in ACTIVE_CLIENTS.items():
        try:
            me = await client.get_me()
            accounts.append({
                "name": name,
                "user_id": me.id,
                "username": me.username,
                "phone": me.phone_number,
                "first_name": me.first_name
            })
        except:
            accounts.append({"name": name, "status": "error"})
    
    return {"active_accounts": accounts}

@app.get("/accounts/{name}/check")
async def check_account(name: str):
    """Проверить статус аккаунта"""
    client = ACTIVE_CLIENTS.get(name)
    if not client:
        raise HTTPException(404, detail="Account not found")
    
    try:
        me = await client.get_me()
        return {
            "status": "active",
            "account": name,
            "user_id": me.id,
            "username": me.username,
            "phone": me.phone_number,
            "first_name": me.first_name,
            "last_name": me.last_name
        }
    except Exception as e:
        return {"status": "error", "account": name, "error": str(e)}

@app.post("/search/by_phone")
async def search_by_phone(req: SearchByPhoneReq, background_tasks: BackgroundTasks):
    """
    Запуск поиска Telegram ID по номерам телефонов
    Результаты придут на указанный webhook_url
    """
    if req.account not in ACTIVE_CLIENTS:
        raise HTTPException(400, detail=f"Account {req.account} not found")
    
    if not req.phones:
        raise HTTPException(400, detail="Phone list is empty")
    
    if not req.webhook_url:
        raise HTTPException(400, detail="webhook_url is required")
    
    # Генерируем task_id если не указан
    task_id = req.task_id or f"search_{int(time.time())}_{hashlib.md5(str(req.phones).encode()).hexdigest()[:8]}"
    
    # Запускаем фоновую задачу
    background_tasks.add_task(
        search_phones_task,
        account_name=req.account,
        phones=req.phones,
        task_id=task_id,
        webhook_url=req.webhook_url,
        delay_between=req.delay_between
    )
    
    logger.info(f"Search task started: {task_id} with {len(req.phones)} phones")
    
    return {
        "status": "search_started",
        "task_id": task_id,
        "account": req.account,
        "total_phones": len(req.phones),
        "webhook_url": req.webhook_url,
        "delay_between": req.delay_between,
        "check_status_url": f"/search/status/{task_id}",
        "message": "Results will be sent to webhook URL"
    }

@app.get("/search/status/{task_id}")
async def get_task_status(task_id: str):
    """Получить статус задачи"""
    task = SEARCH_TASKS.get(task_id)
    if not task:
        raise HTTPException(404, detail="Task not found")
    
    return {
        "task_id": task_id,
        **task
    }

@app.post("/send")
async def send_message(req: SendMessageReq):
    """Отправить сообщение"""
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Account {req.account} not found")
    
    try:
        await client.send_message(req.chat_id, req.text)
        return {"status": "sent", "from": req.account, "to": req.chat_id}
    except Exception as e:
        raise HTTPException(500, detail=f"Send error: {str(e)}")

# ==================== Запуск ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(
        "telegram_bot_pyrogram:app",
        host="0.0.0.0",
        port=port,
        reload=False
    )
