import os
import hashlib
import asyncio
from pathlib import Path
from typing import Dict, Any
from fastapi import FastAPI, HTTPException, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from telethon import events
from telethon.sessions import StringSession
from telethon import TelegramClient
from telethon.tl.types import PeerUser, PeerChannel, PeerChat


# =============================
# ENV
# =============================
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESS_DIR = Path("sessions")
SESS_DIR.mkdir(exist_ok=True)

if not API_ID or not API_HASH:
    raise RuntimeError("Please set API_ID and API_HASH in Render → Environment")


# =============================
# STORAGE
# =============================
clients: Dict[str, TelegramClient] = {}          # accountId -> TelegramClient
queues: Dict[str, Dict[str, asyncio.Queue]] = {} # accountId -> {peerKey: Queue}
temp_hashes: Dict[str, str] = {}                 # phone -> phone_code_hash


# =============================
# FASTAPI + CORS
# =============================
ALLOWED_ORIGINS = [
    # твой прод фронт
    "https://replymaster.top",
    "https://www.replymaster.top",
    # если используешь кастомный домен Vercel
    "https://replymaster-frontend.vercel.app",
    # локальная разработка фронта
    "http://localhost:3000",
]

app = FastAPI(title="Replymaster Telegram API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================
# BASIC ROUTES
# =============================
@app.get("/")
async def root():
    return {"ok": True, "service": "replymaster-api"}

@app.get("/health")
async def health():
    return {"ok": True}


# =============================
# HELPERS
# =============================
def session_path_for(account_id: str) -> Path:
    return SESS_DIR / f"{account_id}.session"

def account_id_from_session(session_str: str) -> str:
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

    p = session_path_for(account_id)
    if not p.exists():
        raise HTTPException(status_code=400, detail="Unknown accountId")

    session_str = p.read_text().strip()
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()
    client.add_event_handler(lambda e: on_new_message(account_id, e), events.NewMessage())
    clients[account_id] = client
    return client

async def on_new_message(account_id: str, event: events.NewMessage.Event):
    try:
        msg = event.message
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

        if pid is not None:
            qmap = queues.get(account_id) or {}
            q = qmap.get(peer_key(pid))
            if q:
                await q.put(payload)
    except Exception:
        # не валим обработчик
        pass


# =============================
# TELEGRAM ROUTES
# =============================
@app.post("/tg/sendCode")
async def tg_send_code(payload: Dict[str, Any] = Body(...)):
    """
    body: { "phone": "+7XXXXXXXXXX" }
    """
    phone = str(payload.get("phone", "")).strip()
    if not phone:
        return JSONResponse(status_code=400, content={"error": "phone required"})

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    try:
        r = await client.send_code_request(phone)
        temp_hashes[phone] = r.phone_code_hash
        return {"phone_code_hash": r.phone_code_hash}
    except Exception as e:
        # всегда возвращаем JSON (иначе на фронте "Unexpected end of JSON input")
        return JSONResponse(status_code=400, content={"error": str(e)})
    finally:
        await client.disconnect()


@app.post("/tg/signIn")
async def tg_sign_in(payload: Dict[str, Any] = Body(...)):
    """
    body: { "phone": "+7XXXXXXXXXX", "code": "12345" }
    """
    phone = str(payload.get("phone", "")).strip()
    code = str(payload.get("code", "")).strip()
    if not phone or not code:
        return JSONResponse(status_code=400, content={"error": "phone & code required"})

    phone_code_hash = temp_hashes.get(phone)
    if not phone_code_hash:
        return JSONResponse(status_code=400, content={"error": "phone_code_hash missing/expired; call /tg/sendCode first"})

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        session_str = client.session.save()
        acc_id = account_id_from_session(session_str)

        session_path_for(acc_id).write_text(session_str)

        if acc_id not in clients:
            c2 = TelegramClient(StringSession(session_str), API_ID, API_HASH)
            await c2.connect()
            c2.add_event_handler(lambda e: on_new_message(acc_id, e), events.NewMessage())
            clients[acc_id] = c2

        return {"accountId": acc_id}
    except Exception as e:
        if "SESSION_PASSWORD_NEEDED" in str(e):
            return JSONResponse(status_code=403, content={"error": "Two-factor password required"})
        return JSONResponse(status_code=400, content={"error": str(e)})
    finally:
        await client.disconnect()


@app.post("/tg/me")
async def tg_me(payload: Dict[str, Any] = Body(...)):
    account_id = str(payload.get("accountId", "")).strip()
    if not account_id:
        return JSONResponse(status_code=400, content={"error": "accountId required"})
    client = await get_or_load_client(account_id)
    await ensure_connected(client)
    me = await client.get_me()
    return {
        "id": int(me.id),
        "username": getattr(me, "username", None),
        "phone": getattr(me, "phone", None),
        "first_name": getattr(me, "first_name", None),
        "last_name": getattr(me, "last_name", None),
    }


@app.post("/tg/dialogs")
async def tg_dialogs(payload: Dict[str, Any] = Body(...)):
    account_id = str(payload.get("accountId", "")).strip()
    limit = int(payload.get("limit", 50))
    if not account_id:
        return JSONResponse(status_code=400, content={"error": "accountId required"})

    client = await get_or_load_client(account_id)
    await ensure_connected(client)

    dialogs = []
    async for d in client.iter_dialogs(limit=limit):
        entity = d.entity
        pid = getattr(entity, "id", None)
        title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or "Dialog"
        is_user = hasattr(entity, "first_name")
        is_group = getattr(entity, "megagroup", False)
        is_channel = getattr(entity, "broadcast", False)
        dialogs.append({
            "peerId": str(pid),
            "title": title,
            "isUser": is_user,
            "isGroup": is_group,
            "isChannel": is_channel,
        })
    return {"dialogs": dialogs}


@app.post("/tg/messages")
async def tg_messages(payload: Dict[str, Any] = Body(...)):
    account_id = str(payload.get("accountId", "")).strip()
    peer_id = str(payload.get("peerId", "")).strip()
    limit = int(payload.get("limit", 100))
    limit = max(1, min(500, limit))
    if not account_id or not peer_id:
        return JSONResponse(status_code=400, content={"error": "accountId & peerId required"})

    client = await get_or_load_client(account_id)
    await ensure_connected(client)
    try:
        entity = await client.get_entity(int(peer_id))
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Cannot resolve peerId"})

    msgs = []
    async for m in client.iter_messages(entity, limit=limit):
        from_id = None
        fid = getattr(m, "from_id", None)
        if hasattr(fid, "user_id"):
            from_id = int(fid.user_id)
        elif hasattr(fid, "channel_id"):
            from_id = int(fid.channel_id)
        msgs.append({
            "id": int(m.id),
            "text": m.message or "",
            "date": m.date.isoformat() if m.date else None,
            "fromId": from_id,
            "peerId": str(peer_id),
            "out": bool(m.out),
        })
    msgs.sort(key=lambda x: x["id"])
    return {"messages": msgs}


@app.get("/tg/subscribe")
async def tg_subscribe(accountId: str = Query(...), peerId: str = Query(...)):
    account_id = accountId.strip()
    peer_id = peerId.strip()
    if not account_id or not peer_id:
        return JSONResponse(status_code=400, content={"error": "accountId & peerId required"})

    client = await get_or_load_client(account_id)
    await ensure_connected(client)

    qmap = queues.setdefault(account_id, {})
    q = asyncio.Queue()
    qmap[peer_key(int(peer_id))] = q

    async def gen():
        try:
            while True:
                data = await q.get()
                yield {"event": "message", "data": data}
        except asyncio.CancelledError:
            try:
                qmap.pop(peer_key(int(peer_id)), None)
            except Exception:
                pass
            raise

    return EventSourceResponse(gen())
