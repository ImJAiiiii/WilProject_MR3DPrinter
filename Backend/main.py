# backend/main.py
from __future__ import annotations

import os
import re
import time
import asyncio
import logging
import base64
from pathlib import Path
from datetime import datetime
from typing import Dict, Set, Optional, List

from dotenv import load_dotenv
from fastapi import (
    FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect,
    Query, APIRouter, Response
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.encoders import jsonable_encoder

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

# -------------------------------------------------------------------
# Bootstrap / ENV / Logging
# -------------------------------------------------------------------
BACKEND_DIR = Path(__file__).resolve().parent
os.chdir(BACKEND_DIR)

load_dotenv()
load_dotenv(BACKEND_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

# -------------------------------------------------------------------
# Create App
# -------------------------------------------------------------------
API_TITLE = "3D Printer Backend (FastAPI)"
API_VERSION = "v2"
app = FastAPI(title=API_TITLE, version=API_VERSION)

# -------------------------------------------------------------------
# CORS
# -------------------------------------------------------------------
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
        "ETag", "Content-Length", "Content-Type", "Content-Disposition",
        "Accept-Ranges", "Content-Range"
    ],
    max_age=3600,
)

# -------------------------------------------------------------------
# Routers
# -------------------------------------------------------------------
from notifications import router as notifications_router, notify_user
from printer_status import router as printer_status_router
from print_queue import router as queue_router
from print_history import router as history_router
from files_api import router as files_router
from print_api import router as print_router
try:
    from custom_storage_s3 import router as storage_router
except Exception:
    from storage import router as storage_router
try:
    from slicer_prusa import router as slicer_router
except Exception:
    from slicer_core import router as slicer_router
try:
    from gpu_preview_vispy import router as preview_router
except Exception:
    preview_router = None
try:
    from files_raw import router as files_raw_router
except Exception:
    files_raw_router = None
try:
    from preview_regen import router as preview_regen_router
except Exception:
    preview_regen_router = None

# -------------------------------------------------------------------
# Auto-chain Daemon
# -------------------------------------------------------------------
import httpx

AUTO_CHAIN_DAEMON = os.getenv("AUTO_CHAIN_DAEMON", "1").strip().lower() not in {"0", "false", "off", "no"}
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
                pass

async def _auto_chain_daemon():
    await asyncio.sleep(2.0)
    while True:
        try:
            await _auto_chain_tick_once()
        except Exception:
            pass
        await asyncio.sleep(AUTO_CHAIN_INTERVAL)

# -------------------------------------------------------------------
# Static Files
# -------------------------------------------------------------------
UPLOADS_DIR = os.getenv("UPLOADS_DIR", "uploads")
UPLOADS_DIR_ABS = str((BACKEND_DIR / UPLOADS_DIR).resolve())

if not os.path.isdir(UPLOADS_DIR_ABS):
    os.makedirs(UPLOADS_DIR_ABS, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR_ABS), name="uploads")

# ✅ === Placeholder Image Setup ===
IMAGES_DIR = str((BACKEND_DIR / "images").resolve())
PLACEHOLDER_NAME = "placeholder-model.png"
_PLACEHOLDER_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB"
    "AgtcE+UAAAAASUVORK5CYII="
)
_PLACEHOLDER_PNG_BYTES = base64.b64decode(_PLACEHOLDER_PNG_BASE64)

# Create /images folder + placeholder if not exist
try:
    os.makedirs(IMAGES_DIR, exist_ok=True)
    ph_path = os.path.join(IMAGES_DIR, PLACEHOLDER_NAME)
    if not os.path.isfile(ph_path):
        with open(ph_path, "wb") as f:
            f.write(_PLACEHOLDER_PNG_BYTES)
except Exception as e:
    logging.warning("[images] cannot create placeholder: %r", e)

# Mount static /images
if os.path.isdir(IMAGES_DIR):
    app.mount("/images", StaticFiles(directory=IMAGES_DIR), name="images")

# Fallback endpoint for /images/*
@app.get("/images/{path:path}")
def _images_any(path: str):
    target = os.path.join(IMAGES_DIR, path)
    if os.path.isfile(target):
        with open(target, "rb") as f:
            data = f.read()
        return Response(content=data, media_type="image/png")
    return Response(content=_PLACEHOLDER_PNG_BYTES, media_type="image/png")

# -------------------------------------------------------------------
# Startup / Shutdown
# -------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    Base.metadata.create_all(bind=engine)
    logging.info("[startup] app ready (origins=%s)", allow_origins)
    if AUTO_CHAIN_DAEMON:
        global _daemon_task
        loop = asyncio.get_event_loop()
        _daemon_task = loop.create_task(_auto_chain_daemon())

@app.on_event("shutdown")
async def shutdown():
    global _daemon_task
    if _daemon_task and not _daemon_task.done():
        _daemon_task.cancel()

# -------------------------------------------------------------------
# Include Routers
# -------------------------------------------------------------------
def include_both(router, *, name: str):
    if router is None:
        logging.warning("[main] skip include: %s", name)
        return
    from fastapi.routing import APIRoute
    paths = []
    for r in getattr(router, "routes", []):
        if isinstance(r, APIRoute):
            paths.append(r.path or "")
    app.include_router(router)
    has_api_prefix = paths and all(p.startswith("/api/") for p in paths if p)
    if not has_api_prefix:
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

# -------------------------------------------------------------------
# Auth / Health / Root
# -------------------------------------------------------------------
@app.post("/auth/login", response_model=LoginOut)
async def login(payload: LoginIn, db: Session = Depends(get_db)):
    raw = (payload.employee_id or "").strip().upper()
    emp = re.sub(r"^EN", "", raw)
    if not re.match(r"^\d{6,7}$", emp):
        raise HTTPException(status_code=422, detail="Invalid Employee ID")

    user = db.query(User).filter(User.employee_id == emp).first()
    if not user:
        raise HTTPException(status_code=404, detail="Employee ID not found")

    access_token = create_access_token(sub=user.employee_id)
    refresh_token = create_refresh_token(sub=user.employee_id)
    user.last_login_at = datetime.utcnow()
    db.add(user); db.commit(); db.refresh(user)

    return LoginOut(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        user=UserOut.model_validate(user),
        needs_confirm=not bool(user.confirmed),
    )

@app.get("/health")
def health():
    return {
        "ok": True,
        "version": API_VERSION,
        "origins": allow_origins,
        "ts": int(time.time()),
    }

# -------------------------------------------------------------------
# Compatibility routes (accept both /api/... and /...)
# -------------------------------------------------------------------
from fastapi.responses import RedirectResponse

@app.get("/storage/{path:path}")
def compat_storage_redirect(path: str, request: Request):
    qs = request.url.query
    url = f"/api/storage/{path}" + (f"?{qs}" if qs else "")
    return RedirectResponse(url=url, status_code=308)

@app.get("/files/{path:path}")
def compat_files_redirect(path: str, request: Request):
    qs = request.url.query
    url = f"/api/files/{path}" + (f"?{qs}" if qs else "")
    return RedirectResponse(url=url, status_code=308)

@app.get("/")
def root():
    return {"name": API_TITLE, "version": API_VERSION}
