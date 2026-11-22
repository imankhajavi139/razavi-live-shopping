import os
import uuid
import json
import hmac
import hashlib
import base64
import secrets
import time

from datetime import datetime, timezone
from pathlib import Path

from fastapi import (
    FastAPI, WebSocket, WebSocketDisconnect,
    HTTPException, Request, Response
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Razavi Live Shopping v0.6")

calls = {}
employee_sockets = set()
customer_sockets = {}

SESSION_COOKIE = "razavi_staff_session"
SESSION_TTL = 8 * 60 * 60
login_attempts = {}


def secret_key():
    value = os.getenv("SESSION_SECRET", "")
    if len(value) < 32:
        raise HTTPException(503, "تنظیم امنیت سرور ناقص است")
    return value.encode()


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def make_session(username):
    csrf = secrets.token_urlsafe(32)
    payload = {
        "username": username,
        "expires": int(time.time()) + SESSION_TTL,
        "csrf": csrf
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    encoded = b64(raw)
    signature = hmac.new(
        secret_key(), encoded.encode(), hashlib.sha256
    ).hexdigest()
    return encoded + "." + signature, csrf


def read_session(request):
    token = request.cookies.get(SESSION_COOKIE, "")
    try:
        encoded, signature = token.rsplit(".", 1)
        expected = hmac.new(
            secret_key(), encoded.encode(), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(signature, expected):
            return None

        raw = base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4)
        )
        data = json.loads(raw)

        if data.get("expires", 0) < int(time.time()):
            return None

        if data.get("username") != os.getenv("EMPLOYEE_USERNAME"):
            return None

        return data
    except Exception:
        return None


def require_employee(request):
    data = read_session(request)
    if not data:
        raise HTTPException(401, "ابتدا وارد پنل کارمند شوید")
    return data


def require_csrf(request, data):
    supplied = request.headers.get("X-CSRF-Token", "")
    if not supplied or not hmac.compare_digest(
        supplied, data.get("csrf", "")
    ):
        raise HTTPException(403, "درخواست امنیتی نامعتبر است")


async def ws_send(ws, data):
    try:
        await ws.send_json(data)
        return True
    except Exception:
        return False


async def broadcast_employee(data):
    for ws in list(employee_sockets):
        if not await ws_send(ws, data):
            employee_sockets.discard(ws)


async def send_customer(cid, data):
    for ws in list(customer_sockets.get(cid, set())):
        if not await ws_send(ws, data):
            customer_sockets.get(cid, set()).discard(ws)


class RequestCall(BaseModel):
    customer_name: str = Field(default="مشتری", max_length=40)


class Action(BaseModel):
    call_id: str


class LoginData(BaseModel):
    username: str
    password: str


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "version": "0.6",
        "livekit_configured": bool(
            os.getenv("LIVEKIT_URL")
            and os.getenv("LIVEKIT_API_KEY")
            and os.getenv("LIVEKIT_API_SECRET")
        ),
        "employee_auth_configured": bool(
            os.getenv("EMPLOYEE_USERNAME")
            and os.getenv("EMPLOYEE_PASSWORD")
            and len(os.getenv("SESSION_SECRET", "")) >= 32
        )
    }


@app.get("/api/auth/status")
def auth_status(request: Request):
    data = read_session(request)
    if not data:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "username": data["username"],
        "csrf": data["csrf"]
    }


@app.post("/api/auth/login")
def employee_login(data: LoginData, request: Request, response: Response):
    username = os.getenv("EMPLOYEE_USERNAME", "")
    password = os.getenv("EMPLOYEE_PASSWORD", "")
        if not username or not password:
        raise HTTPException(503, "تنظیم ورود کارمند انجام نشده است")

    ip = request.client.host if request.client else "unknown"
    now = time.time()
    attempts = login_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < 300]

    if len(attempts) >= 5:
        raise HTTPException(429, "تعداد تلاش زیاد است؛ پنج دقیقه بعد تلاش کنید")

    valid_user = hmac.compare_digest(data.username, username)
    valid_pass = hmac.compare_digest(data.password, password)

    if not (valid_user and valid_pass):
        attempts.append(now)
        login_attempts[ip] = attempts
        raise HTTPException(401, "نام کاربری یا رمز عبور اشتباه است")

    login_attempts.pop(ip, None)
    token, csrf = make_session(username)

    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_TTL,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/"
    )

    return {"ok": True, "csrf": csrf, "username": username}


@app.post("/api/auth/logout")
def employee_logout(request: Request, response: Response):
    data = require_employee(request)
    require_csrf(request, data)
    response.delete_cookie(
        SESSION_COOKIE, path="/", secure=True,
        httponly=True, samesite="strict"
    )
    return {"ok": True}


@app.post("/api/calls")
async def new_call(r: RequestCall):
    cid = uuid.uuid4().hex[:8]
    c = {
        "call_id": cid,
        "customer_name": r.customer_name.strip() or "مشتری",
        "status": "waiting",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "room": "razavi-" + cid
    }
    calls[cid] = c
    await broadcast_employee({"type": "incoming_call", "call": c})
    return c


@app.get("/api/calls")
def waiting(request: Request):
    require_employee(request)
    return [c for c in calls.values() if c["status"] == "waiting"]


@app.post("/api/calls/accept")
async def accept(a: Action, request: Request):
    data = require_employee(request)
    require_csrf(request, data)

    c = calls.get(a.call_id)
    if not c:
        raise HTTPException(404, "درخواست یافت نشد")
    if c["status"] != "waiting":
        raise HTTPException(409, "درخواست قبلاً تعیین تکلیف شده")

    c["status"] = "accepted"
    m = {"type": "call_accepted", "call": c}
    await send_customer(c["call_id"], m)
    await broadcast_employee(m)
    return c


@app.post("/api/calls/reject")
async def reject(a: Action, request: Request):
    data = require_employee(request)
    require_csrf(request, data)

    c = calls.get(a.call_id)
    if not c:
        raise HTTPException(404, "درخواست یافت نشد")
    if c["status"] != "waiting":
        raise HTTPException(409, "درخواست قبلاً تعیین تکلیف شده")

    c["status"] = "rejected"
    await send_customer(
        c["call_id"], {"type": "call_rejected", "call": c}
    )
    await broadcast_employee({"type": "call_rejected", "call": c})
    return c


@app.post("/api/calls/end")
async def end(a: Action, request: Request):
    data = require_employee(request)
    require_csrf(request, data)

    c = calls.get(a.call_id)
    if not c:
        raise HTTPException(404, "درخواست یافت نشد")

    c["status"] = "ended"
    m = {"type": "call_ended", "call": c}
    await send_customer(c["call_id"], m)
    await broadcast_employee(m)
    return c


@app.get("/api/livekit-token")
def token(
    room: str,
    identity: str,
    call_id: str,
    role: str,
    request: Request
):
    c = calls.get(call_id)
    if not c or c["room"] != room or c["status"] != "accepted":
        raise HTTPException(403, "تماس معتبر و پذیرفته‌شده نیست")

    if role not in ("customer", "employee"):
        raise HTTPException(400, "نقش نامعتبر")

    if role == "employee":
        require_employee(request)

    url = os.getenv("LIVEKIT_URL")
    key = os.getenv("LIVEKIT_API_KEY")
    secret = os.getenv("LIVEKIT_API_SECRET")

    if not all((url, key, secret)):
        raise HTTPException(503, "تنظیمات LiveKit کامل نیست")

    try:
        from livekit import api
            safe_identity = role + "-" + uuid.uuid4().hex[:12]
        jwt = (
            api.AccessToken(key, secret)
            .with_identity(safe_identity)
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room,
                    can_publish=True,
                    can_subscribe=True
                )
            )
            .to_jwt()
        )
        return {"server_url": url, "token": jwt}
    except Exception:
        raise HTTPException(500, "ساخت توکن تماس ناموفق شد")


@app.websocket("/ws/employee")
async def employee_ws(ws: WebSocket):
    token = ws.cookies.get(SESSION_COOKIE, "")
    try:
        encoded, signature = token.rsplit(".", 1)
        expected = hmac.new(
            secret_key(), encoded.encode(), hashlib.sha256
        ).hexdigest()
        raw = base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4)
        )
        data = json.loads(raw)

        valid = (
            hmac.compare_digest(signature, expected)
            and data.get("expires", 0) >= int(time.time())
            and data.get("username") == os.getenv("EMPLOYEE_USERNAME")
        )
    except Exception:
        valid = False

    if not valid:
        await ws.close(code=1008)
        return

    await ws.accept()
    employee_sockets.add(ws)
    await ws.send_json({
        "type": "queue_snapshot",
        "calls": [c for c in calls.values() if c["status"] == "waiting"]
    })

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        employee_sockets.discard(ws)


@app.websocket("/ws/customer/{call_id}")
async def customer_ws(ws: WebSocket, call_id: str):
    await ws.accept()
    customer_sockets.setdefault(call_id, set()).add(ws)
    c = calls.get(call_id)

    if c:
        await ws.send_json({"type": "status_snapshot", "call": c})

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        customer_sockets.get(call_id, set()).discard(ws)


@app.get("/")
def home():
    return FileResponse(Path(file).resolve().parent / "index.html")
