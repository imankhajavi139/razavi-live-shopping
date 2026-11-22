import os, uuid, json, hmac, hashlib, base64, secrets, time
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Razavi Live Shopping")
calls = {}
employee_sockets = set()
customer_sockets = {}
attempts = {}
COOKIE = "razavi_staff_session"
TTL = 8 * 60 * 60


def session_key():
    secret = os.getenv("SESSION_SECRET", "")
    if len(secret) < 32:
        raise HTTPException(503, "Session secret is not configured")
    return secret.encode()


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def make_session(username):
    csrf = secrets.token_urlsafe(32)
    payload = {"username": username, "expires": int(time.time()) + TTL, "csrf": csrf}
    encoded = b64(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(session_key(), encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + "." + signature, csrf


def parse_token(token):
    try:
        encoded, signature = token.rsplit(".", 1)
        expected = hmac.new(session_key(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        data = json.loads(raw)
        if data.get("expires", 0) < int(time.time()):
            return None
        if data.get("username") != os.getenv("EMPLOYEE_USERNAME"):
            return None
        return data
    except Exception:
        return None


def get_session(request):
    return parse_token(request.cookies.get(COOKIE, ""))



def require_employee(request):
    data = get_session(request)
    if not data:
        raise HTTPException(401, "Employee login required")
    return data



def check_csrf(request, data):
    if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), data.get("csrf", "")):
        raise HTTPException(403, "Invalid CSRF token")


async def safe_send(ws, data):
    try:
        await ws.send_json(data)
        return True
    except Exception:
        return False


async def broadcast(data):
    for ws in list(employee_sockets):
        if not await safe_send(ws, data):
            employee_sockets.discard(ws)


async def send_customer(call_id, data):
    for ws in list(customer_sockets.get(call_id, set())):
        if not await safe_send(ws, data):
            customer_sockets.get(call_id, set()).discard(ws)


class CallRequest(BaseModel):
    customer_name: str = Field(default="مشتری", max_length=40)


class CallAction(BaseModel):
    call_id: str


class Login(BaseModel):
    username: str
    password: str



@app.get("/api/health")
def health():
    return {
        "ok": True,
        "version": "0.7",
        "livekit_configured": all(os.getenv(k) for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")),
        "employee_auth_configured": bool(os.getenv("EMPLOYEE_USERNAME") and os.getenv("EMPLOYEE_PASSWORD") and len(os.getenv("SESSION_SECRET", "")) >= 32),
    }


@app.get("/api/auth/status")
def auth_status(request: Request):
    data = get_session(request)
    return {"authenticated": bool(data), "username": data.get("username") if data else None, "csrf": data.get("csrf") if data else None}


@app.post("/api/auth/login")
def login(data: Login, request: Request, response: Response):
    username = os.getenv("EMPLOYEE_USERNAME", "")
    password = os.getenv("EMPLOYEE_PASSWORD", "")
    if not username or not password:
        raise HTTPException(503, "Employee login not configured")
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    recent = [t for t in attempts.get(ip, []) if now - t < 300]
    if len(recent) >= 5:
        raise HTTPException(429, "Too many attempts; wait five minutes")
    if not (hmac.compare_digest(data.username, username) and hmac.compare_digest(data.password, password)):
        recent.append(now)
        attempts[ip] = recent
        raise HTTPException(401, "Invalid username or password")
    attempts.pop(ip, None)
    token, csrf = make_session(username)
    response.set_cookie(COOKIE, token, max_age=TTL, httponly=True, secure=True, samesite="strict", path="/")
    return {"ok": True, "csrf": csrf, "username": username}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    data = require_employee(request)
    check_csrf(request, data)
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return {"ok": True}


@app.post("/api/calls")
async def new_call(request_data: CallRequest):
    call_id = uuid.uuid4().hex[:24]
    call = {
        "call_id": call_id,
        "customer_name": request_data.customer_name.strip() or "مشتری",
        "status": "waiting",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "room": "razavi-" + call_id,
    }
    calls[call_id] = call
    await broadcast({"type": "incoming_call", "call": call})
    return call


@app.get("/api/calls")
def waiting_calls(request: Request):
    require_employee(request)
    return [call for call in calls.values() if call["status"] == "waiting"]


@app.post("/api/calls/accept")
async def accept_call(action: CallAction, request: Request):
    data = require_employee(request)
    check_csrf(request, data)
    call = calls.get(action.call_id)
    if not call:
        raise HTTPException(404, "Call not found")
    if call["status"] != "waiting":
        raise HTTPException(409, "Already handled")
    call["status"] = "accepted"
    message = {"type": "call_accepted", "call": call}
    await send_customer(call["call_id"], message)
    await broadcast(message)
    return call


@app.post("/api/calls/reject")
async def reject_call(action: CallAction, request: Request):
    data = require_employee(request)
    check_csrf(request, data)
    call = calls.get(action.call_id)
    if not call:
        raise HTTPException(404, "Call not found")
    if call["status"] != "waiting":
        raise HTTPException(409, "Already handled")
    call["status"] = "rejected"
    message = {"type": "call_rejected", "call": call}
    await send_customer(call["call_id"], message)
    await broadcast(message)
    return call


@app.post("/api/calls/end")
async def end_call(action: CallAction, request: Request):
    data = require_employee(request)
    check_csrf(request, data)
    call = calls.get(action.call_id)
    if not call:
        raise HTTPException(404, "Call not found")
    call["status"] = "ended"
    message = {"type": "call_ended", "call": call}
    await send_customer(call["call_id"], message)
    await broadcast(message)
    return call


@app.get("/api/livekit-token")
def livekit_token(room: str, call_id: str, role: str, request: Request):
    call = calls.get(call_id)
    if not call or call["room"] != room or call["status"] != "accepted":
        raise HTTPException(403, "Call is not accepted")
    if role not in ("customer", "employee"):
        raise HTTPException(400, "Invalid role")
    if role == "employee":
        require_employee(request)
    server_url = os.getenv("LIVEKIT_URL")
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")
    if not all((server_url, api_key, api_secret)):
        raise HTTPException(503, "LiveKit not configured")
    try:
        from livekit import api
        identity = role + "-" + uuid.uuid4().hex[:12]
        token = (api.AccessToken(api_key, api_secret)
                 .with_identity(identity)
                 .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True))
                 .to_jwt())
        return {"server_url": server_url, "token": token}
    except Exception:
        raise HTTPException(500, "Could not create LiveKit token")


@app.websocket("/ws/employee")
async def employee_ws(ws: WebSocket):
    data = parse_token(ws.cookies.get(COOKIE, ""))
    if not data:
        await ws.close(code=1008)
        return
    await ws.accept()
    employee_sockets.add(ws)
    await ws.send_json({"type": "queue_snapshot", "calls": [c for c in calls.values() if c["status"] == "waiting"]})
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
    call = calls.get(call_id)
    if call:
        await ws.send_json({"type": "status_snapshot", "call": call})
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        customer_sockets.get(call_id, set()).discard(ws)


@app.get("/")
def home():
    return FileResponse(Path(__file__).resolve().parent / "index.html")
