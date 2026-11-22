import os, uuid
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv
load_dotenv()
app=FastAPI(title='Razavi Live Shopping v0.5')
calls={}
employee_sockets=set(); customer_sockets={}
async def ws_send(ws, data):
    try: await ws.send_json(data); return True
    except Exception: return False
async def broadcast_employee(data):
    for ws in list(employee_sockets):
        if not await ws_send(ws,data): employee_sockets.discard(ws)
async def send_customer(cid,data):
    for ws in list(customer_sockets.get(cid,set())):
        if not await ws_send(ws,data): customer_sockets.get(cid,set()).discard(ws)
class RequestCall(BaseModel):
    customer_name:str=Field(default='مشتری',max_length=40)
class Action(BaseModel):
    call_id:str
@app.get('/api/health')
def health(): return {'ok':True,'version':'0.5','livekit_configured':bool(os.getenv('LIVEKIT_URL') and os.getenv('LIVEKIT_API_KEY') and os.getenv('LIVEKIT_API_SECRET'))}
@app.post('/api/calls')
async def new_call(r:RequestCall):
    cid=uuid.uuid4().hex[:8]
    c={'call_id':cid,'customer_name':r.customer_name.strip() or 'مشتری','status':'waiting','created_at':datetime.now(timezone.utc).isoformat(),'room':'razavi-'+cid}
    calls[cid]=c; await broadcast_employee({'type':'incoming_call','call':c}); return c
@app.get('/api/calls')
def waiting(): return [c for c in calls.values() if c['status']=='waiting']
@app.post('/api/calls/accept')
async def accept(a:Action):
    c=calls.get(a.call_id)
    if not c: raise HTTPException(404,'درخواست یافت نشد')
    if c['status']!='waiting': raise HTTPException(409,'درخواست قبلاً تعیین تکلیف شده')
    c['status']='accepted'; m={'type':'call_accepted','call':c}; await send_customer(c['call_id'],m); await broadcast_employee(m); return c
@app.post('/api/calls/reject')
async def reject(a:Action):
    c=calls.get(a.call_id)
    if not c: raise HTTPException(404,'درخواست یافت نشد')
    if c['status']!='waiting': raise HTTPException(409,'درخواست قبلاً تعیین تکلیف شده')
    c['status']='rejected'; await send_customer(c['call_id'],{'type':'call_rejected','call':c}); await broadcast_employee({'type':'call_rejected','call':c}); return c
@app.post('/api/calls/end')
async def end(a:Action):
    c=calls.get(a.call_id)
    if not c: raise HTTPException(404,'درخواست یافت نشد')
    c['status']='ended'; m={'type':'call_ended','call':c}; await send_customer(c['call_id'],m); await broadcast_employee(m); return c
@app.get('/api/livekit-token')
def token(room:str, identity:str, call_id:str, role:str):
    c=calls.get(call_id)
    if not c or c['room']!=room or c['status']!='accepted': raise HTTPException(403,'تماس معتبر و پذیرفته‌شده نیست')
    if role not in ('customer','employee'): raise HTTPException(400,'نقش نامعتبر')
    url=os.getenv('LIVEKIT_URL'); key=os.getenv('LIVEKIT_API_KEY'); secret=os.getenv('LIVEKIT_API_SECRET')
    if not all((url,key,secret)): raise HTTPException(503,'تنظیمات LiveKit روی سرور تکمیل نشده')
    try:
        from livekit import api
        safe_identity=role+'-'+uuid.uuid4().hex[:12]
        jwt=api.AccessToken(key,secret).with_identity(safe_identity).with_grants(api.VideoGrants(room_join=True,room=room,can_publish=True,can_subscribe=True)).to_jwt()
        return {'server_url':url,'token':jwt}
    except Exception: raise HTTPException(500,'ساخت توکن تماس ناموفق شد')
@app.websocket('/ws/employee')
async def employee_ws(ws:WebSocket):
    await ws.accept(); employee_sockets.add(ws); await ws.send_json({'type':'queue_snapshot','calls':[c for c in calls.values() if c['status']=='waiting']})
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect: pass
    finally: employee_sockets.discard(ws)
@app.websocket('/ws/customer/{call_id}')
async def customer_ws(ws:WebSocket,call_id:str):
    await ws.accept(); customer_sockets.setdefault(call_id,set()).add(ws); c=calls.get(call_id)
    if c: await ws.send_json({'type':'status_snapshot','call':c})
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect: pass
    finally: customer_sockets.get(call_id,set()).discard(ws)
@app.get('/')
def home():
    return FileResponse(Path(__file__).resolve().parent/'index.html')
