# backend/main.py
from __future__ import annotations

import os
import re
import time
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Set, Optional, List

from dotenv import load_dotenv

# =========================
# Bootstrap / ENV / Logging
# =========================
BACKEND_DIR = Path(__file__).resolve().parent
os.chdir(BACKEND_DIR)

load_dotenv()
load_dotenv(BACKEND_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

# =========================
# FastAPI / Std
# =========================
from fastapi import (
    FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect,
    Query, APIRouter
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.encoders import jsonable_encoder

# =========================
# DB / Models / Auth
# =========================
from sqlalchemy.orm import Session
from db import Base, engine, get_db, SessionLocal
from models import User
from schemas import (
    LoginIn, LoginOut, UserOut, UpdateMeIn,
    RefreshIn, RefreshOut
)
from auth import (
    create_access_token, create_refresh_token, decode_refresh_token,
    get_current_user, decode_token
)

# =========================
# Routers (always-on)
# =========================
from notifications import router as notifications_router, notify_user
from printer_status import router as printer_status_router
from print_queue import router as queue_router
from print_history import router as history_router
from files_api import router as files_router  # legacy /files/*

# =========================
# Storage backend selector
# =========================
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "s3").lower().strip()
storage_router = None
try:
    if STORAGE_BACKEND == "local":
        from storage import router as storage_router  # type: ignore
    else:
        try:
            from custom_storage_s3 import router as storage_router  # type: ignore
        except Exception as e1:
            logging.warning("[main] custom_storage_s3 import failed: %r ; fallback to storage.py", e1)
            from storage import router as storage_router  # type: ignore
except Exception as e:
    logging.error("[main] storage backend import error (%r) ; forcing storage.py", e)
    from storage import router as storage_router  # type: ignore

# =========================
# Slicer (prusa → core fallback)
# =========================
try:
    from slicer_prusa import router as slicer_router  # type: ignore
except Exception as e:
    logging.warning("[main] slicer_prusa import failed (%r) → using slicer_core", e)
    from slicer_core import router as slicer_router  # type: ignore

# =========================
# OctoPrint / Print API
# =========================
from print_api import router as print_router  # type: ignore

# =========================
# Preview (vispy/cpu auto-fallback)
# =========================
PREVIEW_BACKEND = os.getenv("PREVIEW_BACKEND", "vispy").lower().strip()
GPU_PREVIEW_ENABLED = False
preview_router = None
preview_import_errors: List[str] = []

def _try_import_vispy_router():
    try:
        from gpu_preview_vispy import router as _r  # type: ignore
        return _r
    except Exception as e:
        preview_import_errors.append(f"vispy import (root) error: {e!r}")
    try:
        from routers.gpu_preview_vispy import router as _r  # type: ignore
        return _r
    except Exception as e:
        preview_import_errors.append(f"vispy import (routers.pkg) error: {e!r}")
    return None

def _try_import_cpu_router():
    try:
        from gpu_preview_cpu import router as _r  # type: ignore
        return _r
    except Exception as e:
        preview_import_errors.append(f"cpu import (root) error: {e!r}")
    try:
        from routers.gpu_preview_cpu import router as _r  # type: ignore
        return _r
    except Exception as e:
        preview_import_errors.append(f"cpu import (routers.pkg) error: {e!r}")
    return None

if PREVIEW_BACKEND == "vispy":
    preview_router = _try_import_vispy_router() or _try_import_cpu_router()
    PREVIEW_BACKEND = "vispy" if preview_router and "vispy" in getattr(preview_router, "tags", []) else \
                      ("cpu" if preview_router else "none")
else:
    preview_router = _try_import_cpu_router() or _try_import_vispy_router()
    PREVIEW_BACKEND = "cpu" if preview_router and "cpu" in getattr(preview_router, "tags", []) else \
                      ("vispy" if preview_router else "none")

GPU_PREVIEW_ENABLED = preview_router is not None

# =========================
# files_raw (จำเป็นต่อ WebGL/Range)
# =========================
def _try_import_files_raw_router():
    try:
        from files_raw import router as _r  # type: ignore
        return _r
    except Exception:
        pass
    try:
        from routers.files_raw import router as _r  # type: ignore
        return _r
    except Exception:
        return None

files_raw_router = _try_import_files_raw_router()

# =========================
# gcode meta util
# =========================
from gcode_meta import meta_from_gcode_object  # type: ignore

# =========================
# preview_regen (render PNGs)
# =========================
try:
    from preview_regen import router as preview_regen_router  # type: ignore
except Exception as e:
    preview_regen_router = None
    logging.warning("[main] preview_regen import failed: %r", e)

# =========================
# App
# =========================
API_TITLE   = "3D Printer Backend (FastAPI)"
API_VERSION = "v2"

app = FastAPI(title=API_TITLE, version=API_VERSION)

# =========================
# CORS
# =========================
def _parse_origins(val: str) -> List[str]:
    if not val:
        return []
    return [o.strip() for o in val.split(",") if o.strip()]

DEV_DEFAULTS = [
    "http://localhost:3000", "http://127.0.0.1:3000",
    "http://localhost:5173", "http://127.0.0.1:5173",
]
env_origins_raw = os.getenv("CORS_ORIGINS", "").strip()
if env_origins_raw == "*":
    allow_origins = ["*"]; allow_credentials = False
else:
    allow_origins = sorted(set(DEV_DEFAULTS + _parse_origins(env_origins_raw)))
    allow_credentials = True

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "ETag","Content-Length","Content-Type","Content-Disposition",
        "Accept-Ranges","Content-Range"
    ],
    max_age=3600,
)

# =========================
# Auto-chain Daemon (NEW)
# =========================
import httpx

AUTO_CHAIN_DAEMON   = os.getenv("AUTO_CHAIN_DAEMON", "1").strip().lower() not in {"0","false","off","no"}
AUTO_CHAIN_INTERVAL = max(2, int(os.getenv("AUTO_CHAIN_INTERVAL", "5").strip() or "5"))
AUTO_CHAIN_PRINTERS = [p.strip() for p in os.getenv("AUTO_CHAIN_PRINTERS", "prusa-core-one").split(",") if p.strip()]
BACKEND_INTERNAL_BASE = os.getenv("BACKEND_INTERNAL_BASE", "http://127.0.0.1:8001").rstrip("/")

_daemon_task: Optional[asyncio.Task] = None

async def _auto_chain_tick_once():
    async with httpx.AsyncClient(timeout=10.0) as c:
        for pid in AUTO_CHAIN_PRINTERS:
            try:
                await c.get(f"{BACKEND_INTERNAL_BASE}/printers/{pid}/octoprint/job", params={"force": "1"})
            except Exception:
                # keep loop alive
                pass

async def _auto_chain_daemon():
    await asyncio.sleep(2.0)  # wait app ready
    while True:
        try:
            await _auto_chain_tick_once()
        except Exception:
            pass
        await asyncio.sleep(AUTO_CHAIN_INTERVAL)

# =========================
# DB / Static & Startup
# =========================
UPLOADS_DIR = os.getenv("UPLOADS_DIR", "uploads")
UPLOADS_DIR_ABS = str((BACKEND_DIR / UPLOADS_DIR).resolve())

@app.on_event("startup")
async def startup():
    Base.metadata.create_all(bind=engine)
    if STORAGE_BACKEND == "local":
        os.makedirs(UPLOADS_DIR_ABS, exist_ok=True)
    logging.info(
        "[startup] STORAGE_BACKEND=%s  PREVIEW_BACKEND=%s  GPU_PREVIEW_ENABLED=%s  CORS_ORIGINS=%s",
        STORAGE_BACKEND, PREVIEW_BACKEND, GPU_PREVIEW_ENABLED, allow_origins
    )
    # start daemon
    if AUTO_CHAIN_DAEMON:
        global _daemon_task
        loop = asyncio.get_event_loop()
        _daemon_task = loop.create_task(_auto_chain_daemon())

@app.on_event("shutdown")
async def shutdown():
    global _daemon_task
    if _daemon_task and not _daemon_task.done():
        _daemon_task.cancel()

if STORAGE_BACKEND == "local":
    app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR_ABS), name="uploads")

# =========================
# Include Routers (root + /api)
# =========================
def include_both(router, *, name: str):
    if router is None:
        logging.warning("[main] skip include: %s (router is None)", name); return
    app.include_router(router)
    app.include_router(router, prefix="/api", include_in_schema=False)

include_both(notifications_router, name="notifications")
include_both(printer_status_router, name="printer_status")
include_both(queue_router, name="print_queue")
include_both(storage_router, name="storage")
include_both(print_router, name="print_api")
include_both(history_router, name="print_history")
include_both(files_router, name="files_api")
include_both(slicer_router, name="slicer")
include_both(preview_router, name="preview")
include_both(files_raw_router, name="files_raw")
include_both(preview_regen_router, name="preview_regen")

# =========================
# Proxy: /storage/catalog & /api/storage/catalog
# =========================
def _resolve_catalog_handler():
    """defer import; support both custom_storage_s3 and storage"""
    import importlib
    for modname in ("custom_storage_s3", "storage"):
        try:
            mod = importlib.import_module(modname)
            fn = getattr(mod, "list_catalog", None)
            if fn:
                return fn
            logging.warning("[catalog] module '%s' has no list_catalog()", modname)
        except Exception:
            logging.exception("[catalog] import module '%s' failed", modname)
    return None

@app.get("/storage/catalog", tags=["storage"])
def storage_catalog_proxy(
    model: Optional[str] = Query(None, description="DELTA | HONTECH | (เว้นว่าง=ทั้งหมด)"),
    q: Optional[str] = Query(None, description="ค้นหาชื่อ"),
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=2000),
    with_urls: bool = Query(False),
    with_head: bool = Query(False),
    db: Session = Depends(get_db),
    _me: User = Depends(get_current_user),
):
    fn = _resolve_catalog_handler()
    if not fn:
        raise HTTPException(status_code=501, detail="catalog endpoint not implemented in storage backend")
    try:
        return fn(model=model, q=q, offset=offset, limit=limit, with_urls=with_urls, with_head=with_head, db=db, _me=_me)
    except Exception as e:
        logging.exception("list_catalog failed")
        raise HTTPException(status_code=500, detail=f"catalog error: {e!r}")

@app.get("/api/storage/catalog", tags=["storage"])
def storage_catalog_api_proxy(
    model: Optional[str] = Query(None, description="DELTA | HONTECH | (เว้นว่าง=ทั้งหมด)"),
    q: Optional[str] = Query(None, description="ค้นหาชื่อ"),
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=2000),
    with_urls: bool = Query(False),
    with_head: bool = Query(False),
    db: Session = Depends(get_db),
    _me: User = Depends(get_current_user),
):
    fn = _resolve_catalog_handler()
    if not fn:
        raise HTTPException(status_code=501, detail="catalog endpoint not implemented in storage backend")
    try:
        return fn(model=model, q=q, offset=offset, limit=limit, with_urls=with_urls, with_head=with_head, db=db, _me=_me)
    except Exception as e:
        logging.exception("list_catalog failed")
        raise HTTPException(status_code=500, detail=f"catalog error: {e!r}")

# =========================
# /api/gcode/meta
# =========================
gcode_router = APIRouter(prefix="/api/gcode", tags=["gcode"])

@gcode_router.get("/meta")
def gcode_meta(object_key: str):
    key = (object_key or "").strip()
    if not key.lower().endswith((".gcode", ".gco", ".gc")):
        raise HTTPException(status_code=400, detail="object_key must be a G-code file")
    meta = meta_from_gcode_object(key) or {}
    return {
        "time_min":   meta.get("time_min"),
        "time_text":  meta.get("time_text"),
        "filament_g": meta.get("filament_g"),
    }

app.include_router(gcode_router)

# =========================
# WebSocket Hub
# =========================
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

def _token_from_auth_header(header: Optional[str]) -> Optional[str]:
    if not header:
        return None
    parts = header.split()
    return parts[1] if (len(parts) == 2 and parts[0].lower() == "bearer") else None

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

# =========================
# Auth (access + refresh)
# =========================
@app.post("/auth/login", response_model=LoginOut)
async def login(payload: LoginIn, db: Session = Depends(get_db)):
    raw = (payload.employee_id or "").strip().upper()
    emp = re.sub(r"^EN", "", raw)
    if not _DIGITS_RE.match(emp):
        raise HTTPException(status_code=422, detail="Invalid Employee ID (6–7 digits)")

    user = db.query(User).filter(User.employee_id == emp).first()
    if not user:
        raise HTTPException(status_code=404, detail="Employee ID not found")

    access_token = create_access_token(sub=user.employee_id)
    refresh_token = create_refresh_token(sub=user.employee_id)

    needs_confirm = not bool(user.confirmed)
    if not needs_confirm:
        user.last_login_at = datetime.utcnow()
        db.add(user); db.commit(); db.refresh(user)

    # ทำให้เข้ากับ schemas.LoginOut ทั้ง 2 รูปแบบ:
    # - แบบที่ใช้ access_token/refresh_token
    # - แบบที่ใช้ token (ไม่มี refresh)
    try:
        return LoginOut(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            user=UserOut.model_validate(user),
            needs_confirm=needs_confirm,
        )
    except Exception:
        # ถ้า model คาดหวังฟิลด์ token แทน
        return LoginOut(
            token=access_token,
            token_type="bearer",
            user=UserOut.model_validate(user),
            needs_confirm=needs_confirm,
        )

@app.post("/auth/refresh", response_model=RefreshOut)
async def refresh(payload: RefreshIn):
    data = decode_refresh_token(payload.refresh_token)
    sub = data.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    new_access = create_access_token(sub=sub)
    return RefreshOut(access_token=new_access, token_type="bearer")

@app.post("/auth/logout")
async def logout(_: User = Depends(get_current_user)):
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

# =========================
# Demo notify
# =========================
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

# =========================
# Health
# =========================
def _health_payload():
    return {
        "ok": True,
        "version": API_VERSION,
        "origins": allow_origins,
        "storage_backend": STORAGE_BACKEND,
        "uploads_dir": UPLOADS_DIR_ABS,
        "gpu_preview_enabled": GPU_PREVIEW_ENABLED,
        "preview_backend": PREVIEW_BACKEND,
        "preview_import_errors": preview_import_errors,
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

# mirror under /api/*
app.add_api_route("/api/health", health, methods=["GET"], include_in_schema=False)
app.add_api_route("/api/healthz", healthz, methods=["GET"], include_in_schema=False)
app.add_api_route("/api/healthz/live", healthz_live, methods=["GET"], include_in_schema=False)
app.add_api_route("/api/healthz/ready", healthz_ready, methods=["GET"], include_in_schema=False)

# =========================
# Debug: list all routes
# =========================
from fastapi.routing import APIRoute

@app.get("/debug/routes")
def debug_routes():
    items = []
    for r in app.router.routes:
        items.append({
            "type": type(r).__name__,
            "path": getattr(r, "path", None),
            "name": getattr(r, "name", None),
            "methods": sorted(list(getattr(r, "methods", set()) or [])),
        })
    return items

# =========================
# Root
# =========================
@app.get("/")
def root():
    return {"name": API_TITLE, "version": API_VERSION}
