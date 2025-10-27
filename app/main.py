import os
import hashlib
import asyncio
from pathlib import Path
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, HTTPException, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from telethon import events
from telethon.sessions import StringSession
from telethon import TelegramClient
from telethon.tl.types import PeerUser, PeerChannel, PeerChat

# ========= ENV & STORAGE =========
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESS_DIR = Path("sessions")
SESS_DIR.mkdir(exist_ok=True)

if not API_ID or not API_HASH:
    raise RuntimeError("Please set API_ID and API_HASH in environment")

# В памяти процесса: accountId -> client & helpers
clients: Dict[str, TelegramClient] = {}
queues: Dict[str, Dict[str, asyncio.Queue]] = {}  # accountId -> {peerKey: Queue}

# Временное хранение phone_code_hash: phone -> hash
temp_hashes: Dict[str, str] = {}

app = FastAPI(title="Replymaster TG API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # при необходимости сузить
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========= UTILS =========
def session_path_for(account_id: str) -> Path:
    return SESS_DIR / f"{account_id}.session"

def account_id_from_session(session_str: str) -> str:
    # стабильный детерминированный id из сессии
    h = hashlib.sha1(session_str.encode("utf-8")).hexdigest()[:16]
    return f"acc_{h}"

async def ensure_connected(client: TelegramClient):
    if not client.is_connected():
        await client.connect()

def peer_key(peer_id: int) -> str:
    return str(peer_id)

async def get_or_load_client(account_id: str) -> TelegramClient:
    if account_id in clients:
        await ensure_connected(clients[account_id])
        return clients[account_id]
    # пробуем загрузить сессию с диска
    p = session_path_for(account_id)
    if not p.exists():
        raise HTTPException(status_code=400, detail="Unknown accountId")
    session_str = p.read_text().strip()
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()
    # навешиваем обработчик новых сообщений один раз
    client.add_event_handler(lambda e: on_new_message(account_id, e), events.NewMessage())
    clients[account_id] = client
    return client

async def on_new_message(account_id: str, event: events.NewMessage.Event):
    try:
        msg = event.message
        # Определяем peerId (кому пришло сообщение)
        to = msg.peer_id
        if isinstance(to, PeerUser):
            pid = to.user_id
        elif isinstance(to, PeerChannel):
            pid = to.channel_id
        elif isinstance(to, PeerChat):
            pid = to.chat_id
        else:
            pid = None

        payload = {
            "id": int(msg.id),
            "text": msg.message or "",
            "date": msg.date.isoformat() if msg.date else None,
            "fromId": int(getattr(getattr(msg, "from_id", None), "user_id", 0) or 0),
            "peerId": str(pid) if pid is not None else None,
            "out": bool(msg.out),
        }

        # Если есть подписчики на этот peer — послать в их очереди
        if pid is not None:
            qmap = queues.get(account_id) or {}
            q = qmap.get(peer_key(pid))
            if q:
                await q.put(payload)
    except Exception:
        # не падаем из-за обработчика
        pass

# ========= ROUTES =========

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/tg/sendCode")
async def tg_send_code(payload: Dict[str, Any] = Body(...)):
    phone = str(payload.get("phone", "")).strip()
    if not phone:
        raise HTTPException(400, "phone required")

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    try:
        r = await client.send_code_request(phone)
        # telethon вернёт объект с .phone_code_hash
        temp_hashes[phone] = r.phone_code_hash
        return {"phone_code_hash": r.phone_code_hash}
    finally:
        await client.disconnect()

@app.post("/tg/signIn")
async def tg_sign_in(payload: Dict[str, Any] = Body(...)):
    phone = str(payload.get("phone", "")).strip()
    code = str(payload.get("code", "")).strip()
    if not phone or not code:
        raise HTTPException(400, "phone & code required")

    phone_code_hash = temp_hashes.get(phone)
    if not phone_code_hash:
        raise HTTPException(400, "phone_code_hash missing/expired; call /tg/sendCode first")

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    try:
        # Вход по коду
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        session_str = client.session.save()
        acc_id = account_id_from_session(session_str)
        # Сохраняем строку сессии на диск, чтобы переживать рестарты
        session_path_for(acc_id).write_text(session_str)
        # Создаём "постоянный" клиент и подписываемся на новые сообщения
        if acc_id not in clients:
            c2 = TelegramClient(StringSession(session_str), API_ID, API_HASH)
            await c2.connect()
            c2.add_event_handler(lambda e: on_new_message(acc_id, e), events.NewMessage())
            clients[acc_id] = c2
        return {"accountId": acc_id}
    except Exception as e:
        # Two-factor (password) не реализован в демо
        if "SESSION_PASSWORD_NEEDED" in str(e):
            raise HTTPException(403, "Two-factor password required (not implemented in demo)")
        raise
    finally:
        await client.disconnect()

@app.post("/tg/dialogs")
async def tg_dialogs(payload: Dict[str, Any] = Body(...)):
    account_id = str(payload.get("accountId", "")).strip()
    limit = int(payload.get("limit", 50))
    if not account_id:
        raise HTTPException(400, "accountId required")
    client = await get_or_load_client(account_id)
    await ensure_connected(client)

    dialogs = []
    async for d in client.iter_dialogs(limit=limit):
        entity = d.entity
        # peerId
        pid = None
        if hasattr(entity, "id"):
            pid = int(entity.id)
        title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or "Dialog"
        is_user = hasattr(entity, "first_name")
        is_group = getattr(entity, "megagroup", False)
        is_channel = getattr(entity, "broadcast", False)

        dialogs.append({
            "peerId": str(pid) if pid is not None else None,
            "title": title,
            "isUser": bool(is_user),
            "isGroup": bool(is_group),
            "isChannel": bool(is_channel),
        })
    return {"dialogs": dialogs}

@app.post("/tg/messages")
async def tg_messages(payload: Dict[str, Any] = Body(...)):
    account_id = str(payload.get("accountId", "")).strip()
    peer_id = str(payload.get("peerId", "")).strip()
    limit = int(payload.get("limit", 100))
    limit = max(1, min(500, limit))
    if not account_id or not peer_id:
        raise HTTPException(400, "accountId & peerId required")

    client = await get_or_load_client(account_id)
    await ensure_connected(client)

    # Разрешаем peer по ID
    # Telethon умеет get_entity(int_id)
    try:
        entity = await client.get_entity(int(peer_id))
    except Exception:
        raise HTTPException(400, "Cannot resolve peerId")

    msgs = []
    async for m in client.iter_messages(entity, limit=limit):
        from_id = None
        fid = getattr(m, "from_id", None)
        if hasattr(fid, "user_id"): from_id = int(fid.user_id)
        elif hasattr(fid, "channel_id"): from_id = int(fid.channel_id)
        msgs.append({
            "id": int(m.id),
            "text": m.message or "",
            "date": m.date.isoformat() if m.date else None,
            "fromId": from_id,
            "peerId": str(peer_id),
            "out": bool(m.out),
        })
    # По умолчанию Telethon идёт от новых к старым — отсортируем по возрастанию id
    msgs.sort(key=lambda x: x["id"])
    return {"messages": msgs}

@app.get("/tg/subscribe")
async def tg_subscribe(
    accountId: str = Query(..., alias="accountId"),
    peerId: str = Query(..., alias="peerId"),
):
    """
    Server-Sent Events: real-time поток новых сообщений указанного peer.
    """
    account_id = accountId.strip()
    peer_id = peerId.strip()
    if not account_id or not peer_id:
        raise HTTPException(400, "accountId & peerId required")

    client = await get_or_load_client(account_id)
    await ensure_connected(client)

    # Создаём очередь для этого подписчика
    qmap = queues.setdefault(account_id, {})
    q = asyncio.Queue()
    qmap[peer_key(int(peer_id))] = q

    async def gen():
        try:
            while True:
                data = await q.get()
                yield {"event": "message", "data": data}
        except asyncio.CancelledError:
            # отписываемся
            try:
                qmap.pop(peer_key(int(peer_id)), None)
            except Exception:
                pass
            raise

    return EventSourceResponse(gen())

