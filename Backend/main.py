# backend/main.py
from __future__ import annotations

import os
import re
import time
import asyncio  # [+] ใช้กับ daemon
from pathlib import Path
from datetime import datetime
from typing import Dict, Set, Optional, List

from dotenv import load_dotenv

# ----- load .env (ทั้งโปรเจกต์ + โฟลเดอร์ backend) -----
_BACKEND_DIR = Path(__file__).resolve().parent
load_dotenv()
load_dotenv(_BACKEND_DIR / ".env")

from fastapi import (
    FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect,
    Query, APIRouter
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.encoders import jsonable_encoder

from sqlalchemy.orm import Session

from db import Base, engine, get_db, SessionLocal
from models import User
from schemas import LoginIn, LoginOut, UserOut, UpdateMeIn
from auth import create_access_token, get_current_user, decode_token

# ---------- Routers หลัก ----------
from notifications import router as notifications_router, notify_user
from printer_status import router as printer_status_router
from print_queue import router as queue_router
from print_history import router as history_router
from files_api import router as files_router  # /files/* ชุดเดิม

# ---------- Storage backend selector ----------
_STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "s3").lower()
try:
    if _STORAGE_BACKEND == "local":
        from storage import router as storage_router
    else:
        try:
            from custom_storage_s3 import router as storage_router  # type: ignore
        except Exception:
            from storage import router as storage_router
except Exception:
    from storage import router as storage_router

# ---------- Slicer ----------
try:
    from slicer_prusa import router as slicer_router
except ImportError:
    from slicer_core import router as slicer_router  # fallback

# ---------- OctoPrint ----------
from print_api import router as print_router

# ---------- Preview (เลือกได้ vispy/cpu ด้วย ENV: PREVIEW_BACKEND) ----------
_PREVIEW_BACKEND = os.getenv("PREVIEW_BACKEND", "vispy").lower().strip()
_GPU_PREVIEW_ENABLED = False

preview_router = None
_preview_import_errors: List[str] = []

def _try_import_vispy_router():
    """รองรับ 2 ตำแหน่ง: gpu_preview_vispy หรือ routers.gpu_preview_vispy"""
    try:
        from gpu_preview_vispy import router as _router  # type: ignore
        return _router
    except Exception as e:
        _preview_import_errors.append(f"vispy import (root) error: {e!r}")
    try:
        from routers.gpu_preview_vispy import router as _router  # type: ignore
        return _router
    except Exception as e:
        _preview_import_errors.append(f"vispy import (routers.pkg) error: {e!r}")
    return None

def _try_import_cpu_router():
    """รองรับ 2 ตำแหน่ง: gpu_preview_cpu หรือ routers.gpu_preview_cpu"""
    try:
        from gpu_preview_cpu import router as _router  # type: ignore
        return _router
    except Exception as e:
        _preview_import_errors.append(f"cpu import (root) error: {e!r}")
    try:
        from routers.gpu_preview_cpu import router as _router  # type: ignore
        return _router
    except Exception as e:
        _preview_import_errors.append(f"cpu import (routers.pkg) error: {e!r}")
    return None

# ---------- Files Raw (สำคัญมากสำหรับ WebGL preview) ----------
def _try_import_files_raw_router():
    """รองรับ 2 ตำแหน่ง: files_raw หรือ routers.files_raw"""
    try:
        from files_raw import router as _router  # type: ignore
        return _router
    except Exception:
        pass
    try:
        from routers.files_raw import router as _router  # type: ignore
        return _router
    except Exception:
        return None

# เลือก preview backend
if _PREVIEW_BACKEND == "vispy":
    preview_router = _try_import_vispy_router()
    if preview_router is None:
        preview_router = _try_import_cpu_router()
        _PREVIEW_BACKEND = "cpu" if preview_router else "none"
else:
    preview_router = _try_import_cpu_router()
    if preview_router is None:
        preview_router = _try_import_vispy_router()
        _PREVIEW_BACKEND = "vispy" if preview_router else "none"

_GPU_PREVIEW_ENABLED = preview_router is not None

# ---------- G-code meta ----------
from gcode_meta import meta_from_gcode_object

API_TITLE   = "3D Printer Backend (FastAPI)"
API_VERSION = "v2"

app = FastAPI(title=API_TITLE, version=API_VERSION)

# ---------- CORS ----------
def _parse_origins(env_val: str) -> List[str]:
    if not env_val:
        return []
    return [o.strip() for o in env_val.split(",") if o.strip()]

DEV_DEFAULTS = [
    "http://localhost:3000", "http://127.0.0.1:3000",
    "http://localhost:5173", "http://127.0.0.1:5173",
]
env_origins_raw = os.getenv("CORS_ORIGINS", "").strip()

if env_origins_raw == "*":
    ALLOW_ORIGINS = ["*"]
    ALLOW_CREDENTIALS = False
else:
    ALLOW_ORIGINS = sorted(set(DEV_DEFAULTS + _parse_origins(env_origins_raw)))
    ALLOW_CREDENTIALS = True

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],  # Authorization, Range, Content-Type ฯลฯ
    expose_headers=[
        "ETag", "Content-Length", "Content-Type", "Content-Disposition",
        "Accept-Ranges", "Content-Range",
    ],
    max_age=3600,
)

# =========================
# Auto-chain Daemon (ใหม่)
# =========================
# เคาะ /printers/{pid}/octoprint/job?force=1 เป็นระยะ
# เพื่อให้ logic auto-heal/auto-chain ฝั่ง printer_status ทำงาน
import httpx  # ใช้เรียก HTTP ภายใน

_AUTO_CHAIN_DAEMON   = os.getenv("AUTO_CHAIN_DAEMON", "1").strip().lower() not in {"0", "false", "off", "no"}
_AUTO_CHAIN_INTERVAL = max(2, int(os.getenv("AUTO_CHAIN_INTERVAL", "5").strip() or "5"))
_AUTO_CHAIN_PRINTERS = [p.strip() for p in os.getenv("AUTO_CHAIN_PRINTERS", "prusa-core-one").split(",") if p.strip()]
_INTERNAL_BASE       = os.getenv("BACKEND_INTERNAL_BASE", "http://127.0.0.1:8001").rstrip("/")

_daemon_task: Optional[asyncio.Task] = None

async def _auto_chain_tick_once():
    async with httpx.AsyncClient(timeout=10.0) as c:
        for pid in _AUTO_CHAIN_PRINTERS:
            try:
                # เรียกให้ printer_status.octoprint_job ทำงาน (force)
                await c.get(f"{_INTERNAL_BASE}/printers/{pid}/octoprint/job", params={"force": "1"})
            except Exception:
                # กลืน error ไว้ เพื่อให้วงลูปเดินต่อ
                pass

async def _auto_chain_daemon():
    # รอระบบขึ้นครบก่อน
    await asyncio.sleep(2.0)
    while True:
        try:
            await _auto_chain_tick_once()
        except Exception:
            pass
        await asyncio.sleep(_AUTO_CHAIN_INTERVAL)

@app.on_event("startup")
async def _start_auto_chain_daemon():
    if _AUTO_CHAIN_DAEMON:
        global _daemon_task
        loop = asyncio.get_event_loop()
        _daemon_task = loop.create_task(_auto_chain_daemon())

@app.on_event("shutdown")
async def _stop_auto_chain_daemon():
    global _daemon_task
    if _daemon_task and not _daemon_task.done():
        _daemon_task.cancel()

# ---------- DB & Static Mount ----------
UPLOADS_DIR = os.getenv("UPLOADS_DIR", "uploads")
_UPLOADS_DIR_ABS = str((_BACKEND_DIR / UPLOADS_DIR).resolve())

@app.on_event("startup")
def _on_startup():
    Base.metadata.create_all(bind=engine)
    if _STORAGE_BACKEND == "local":
        os.makedirs(_UPLOADS_DIR_ABS, exist_ok=True)

# เสิร์ฟไฟล์ดิบภายใต้ /uploads/* เฉพาะโหมด local
if _STORAGE_BACKEND == "local":
    app.mount("/uploads", StaticFiles(directory=_UPLOADS_DIR_ABS), name="uploads")

# ---------- Include Routers (root + /api เพื่อเข้ากันได้กับ frontend เก่า) ----------
for rtr in (
    notifications_router, printer_status_router, queue_router,
    storage_router, print_router, history_router, files_router
):
    app.include_router(rtr)
    app.include_router(rtr, prefix="/api", include_in_schema=False)

# slicer
app.include_router(slicer_router)
app.include_router(slicer_router, prefix="/api", include_in_schema=False)

# preview
if _GPU_PREVIEW_ENABLED and preview_router is not None:
    app.include_router(preview_router)
    app.include_router(preview_router, prefix="/api", include_in_schema=False)

# files_raw (จำเป็นต่อ G-code WebGL Viewer)
_files_raw_router = _try_import_files_raw_router()
if _files_raw_router is not None:
    app.include_router(_files_raw_router)  # /files/raw
    app.include_router(_files_raw_router, prefix="/api", include_in_schema=False)

# ---------- G-code meta (REST) ----------
gcode_router = APIRouter(prefix="/api/gcode", tags=["gcode"])

@gcode_router.get("/meta")
def gcode_meta(object_key: str):
    if not object_key or not object_key.lower().endswith((".gcode", ".gco", ".gc")):
        raise HTTPException(status_code=400, detail="object_key must be a G-code file")
    meta = meta_from_gcode_object(object_key)
    return {
        "time_min":   meta.get("time_min"),
        "time_text":  meta.get("time_text"),
        "filament_g": meta.get("filament_g"),
    }

app.include_router(gcode_router)

# ---------- WebSocket Manager ----------
class WSManager:
    def __init__(self):
        self.active_by_emp: Dict[str, Set[WebSocket]] = {}

    async def connect(self, emp: str, ws: WebSocket):
        self.active_by_emp.setdefault(emp, set()).add(ws)

    def disconnect(self, emp: str, ws: WebSocket):
        conns = self.active_by_emp.get(emp)
        if not conns:
            return
        conns.discard(ws)
        if not conns:
            self.active_by_emp.pop(emp, None)

    async def send_to_emp(self, emp: str, message: dict):
        for ws in list(self.active_by_emp.get(emp, set())):
            try:
                await ws.send_json(jsonable_encoder(message))
            except WebSocketDisconnect:
                self.disconnect(emp, ws)
            except Exception:
                self.disconnect(emp, ws)

manager = WSManager()
_DIGITS_RE = re.compile(r"^\d{6,7}$")

# ---------- REST: Auth ----------
@app.post("/auth/login", response_model=LoginOut)
async def login(payload: LoginIn, db: Session = Depends(get_db)):
    raw = payload.employee_id.strip().upper()
    emp = re.sub(r"^EN", "", raw)
    if not _DIGITS_RE.match(emp):
        raise HTTPException(status_code=422, detail="Invalid Employee ID (6–7 digits)")

    user = db.query(User).filter(User.employee_id == emp).first()
    if not user:
        raise HTTPException(status_code=404, detail="Employee ID not found")

    token = create_access_token(sub=user.employee_id)
    needs_confirm = not bool(user.confirmed)
    if not needs_confirm:
        user.last_login_at = datetime.utcnow()
        db.add(user); db.commit(); db.refresh(user)

    return LoginOut(
        token=token,
        token_type="bearer",
        user=UserOut.model_validate(user),
        needs_confirm=needs_confirm,
    )

@app.post("/auth/logout")
async def logout(current: User = Depends(get_current_user)):
    return {"ok": True}

@app.get("/auth/me", response_model=UserOut)
def me(current: User = Depends(get_current_user)):
    return UserOut.model_validate(current)

@app.put("/users/me", response_model=UserOut)
async def update_me(
    data: UpdateMeIn,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    current.name = data.name
    current.email = data.email
    current.confirmed = True
    current.last_login_at = datetime.utcnow()
    db.add(current); db.commit(); db.refresh(current)

    await manager.send_to_emp(
        current.employee_id,
        {"type": "user", "user": UserOut.model_validate(current)}
    )
    return UserOut.model_validate(current)

# ---------- WebSocket ----------
def _token_from_auth_header(header: Optional[str]) -> Optional[str]:
    if not header:
        return None
    parts = header.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, token: Optional[str] = Query(default=None)):
    await websocket.accept()

    auth_header = websocket.headers.get("authorization")
    token = token or _token_from_auth_header(auth_header)

    try:
        if not token:
            await websocket.close(code=4401); return
        payload = decode_token(token)
        emp = payload.get("sub")
        if not emp:
            await websocket.close(code=4401); return
    except Exception:
        await websocket.close(code=4401); return

    await manager.connect(emp, websocket)

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.employee_id == emp).first()
        if user:
            await manager.send_to_emp(emp, {"type": "user", "user": UserOut.model_validate(user)})
    finally:
        db.close()

    try:
        while True:
            _ = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(emp, websocket)
    except Exception:
        manager.disconnect(emp, websocket)

# ---------- Demo ----------
@app.post("/_demo/notify/ok")
async def demo_notify_ok(db: Session = Depends(get_db), current: User = Depends(get_current_user)):
    await notify_user(
        db, current.employee_id,
        type="print.completed", severity="success",
        title="งานพิมพ์เสร็จ", message="ชิ้นงานของคุณพิมพ์เสร็จเรียบร้อย 🎉",
        data={"job": "demo", "result": "success"},
    )
    return {"ok": True}

@app.post("/_demo/notify/fail")
async def demo_notify_fail(db: Session = Depends(get_db), current: User = Depends(get_current_user)):
    await notify_user(
        db, current.employee_id,
        type="print.failed", severity="error",
        title="พิมพ์ไม่สำเร็จ", message="เครื่องรายงานว่ามีข้อผิดพลาดระหว่างพิมพ์",
        data={"job": "demo", "result": "failed"},
    )
    return {"ok": True}

# ---------- Health ----------
def _health_payload():
    return {
        "ok": True,
        "version": API_VERSION,
        "origins": ALLOW_ORIGINS,
        "storage_backend": _STORAGE_BACKEND,
        "uploads_dir": _UPLOADS_DIR_ABS,
        "gpu_preview_enabled": _GPU_PREVIEW_ENABLED,
        "preview_backend": _PREVIEW_BACKEND,
        "preview_import_errors": _preview_import_errors,
        "ts": int(time.time()),
    }

@app.get("/health")
def health():
    return _health_payload()

@app.get("/healthz")
def healthz():
    return _health_payload()

@app.get("/healthz/live")
def healthz_live():
    return {"ok": True, "ts": int(time.time())}

@app.get("/healthz/ready")
def healthz_ready():
    return _health_payload()

app.add_api_route("/api/health", health, methods=["GET"], include_in_schema=False)
app.add_api_route("/api/healthz", healthz, methods=["GET"], include_in_schema=False)
app.add_api_route("/api/healthz/live", healthz_live, methods=["GET"], include_in_schema=False)
app.add_api_route("/api/healthz/ready", healthz_ready, methods=["GET"], include_in_schema=False)

# ---------- Root ----------
@app.get("/")
def root():
    return {"name": API_TITLE, "version": API_VERSION}
