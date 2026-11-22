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
