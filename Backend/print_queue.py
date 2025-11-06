# backend/print_queue.py
from __future__ import annotations

import os
import re
import json
import asyncio
import inspect
import mimetypes
import logging
import time
import uuid
from threading import Lock
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Tuple, Callable, Any
from urllib.parse import urlencode, quote  # ✅ add quote สำหรับ map object key → URL

import httpx
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Header, Query
from fastapi import Request  # NEW: for reading headers (X-Reason)
from sqlalchemy import case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db import get_db
from auth import (
    get_current_user,
    get_confirmed_user,
    get_manager_user,
    get_optional_user,  # ให้ list_queue ใช้งานได้ทั้ง auth / header admin
)
from models import User, Printer, PrintJob, StorageFile
from schemas import (
    PrintJobCreate, PrintJobPatch, PrintJobOut,
    QueueListOut, CurrentJobOut, QueueReorderIn, FinalizeIn,
)

# ----------------------------- Notifications ---------------------------------
# notify_job_event: DM ไปหา "ผู้กดพิมพ์" (flow เดิมฝั่งเว็บ/Teams)
# notify_user: แจ้งเตือนทั่วไป (ใช้บนสถานี/Unity/Holo/เว็บ)
try:
    from notifications import notify_job_event  # type: ignore
except Exception:  # pragma: no cover
    async def notify_job_event(*args, **kwargs):  # type: ignore
        return None

try:
    from notifications import notify_user  # type: ignore
except Exception:  # pragma: no cover
    async def notify_user(*args, **kwargs):  # type: ignore
        return None

# ตัวช่วย fire-and-forget ที่ปลอดภัยกับทั้ง sync/async (ไม่ block main thread)
def _bgcall(func_or_coro, /, *args, **kwargs):
    """
    รับทั้ง:
      - coroutine object    -> schedule ทันที
      - async def function  -> สร้าง task
      - sync function       -> ส่งไปรันใน thread pool
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    # case: รับเป็น coroutine object มาเลย
    if inspect.iscoroutine(func_or_coro):
        if loop:
            loop.create_task(func_or_coro)
        else:
            import threading
            threading.Thread(target=lambda: asyncio.run(func_or_coro), daemon=True).start()
        return

    # case: เป็นฟังก์ชัน (async หรือ sync)
    if inspect.iscoroutinefunction(func_or_coro):
        if loop:
            loop.create_task(func_or_coro(*args, **kwargs))
        else:
            import threading
            threading.Thread(target=lambda: asyncio.run(func_or_coro(*args, **kwargs)), daemon=True).start()
        return

    # sync function
    if loop:
        loop.run_in_executor(None, lambda: func_or_coro(*args, **kwargs))
    else:
        import threading
        threading.Thread(target=lambda: func_or_coro(*args, **kwargs), daemon=True).start()

# ------------------------------- S3 helpers ----------------------------------
from s3util import (
    copy_object, head_object, delete_object, new_storage_key, presign_get,
)

# optional put_object
try:
    from s3util import put_object  # type: ignore
except Exception:  # pragma: no cover
    put_object = None

# optional finalize helper (แพ็กเกจ/ไฟล์เดียว → catalog/<Model>/...)
try:
    from .custom_storage_s3 import finalize_to_storage  # type: ignore
except Exception:
    try:
        from custom_storage_s3 import finalize_to_storage  # type: ignore
    except Exception:
        finalize_to_storage = None

# optional preview renderer
_HAS_RENDERER = False
try:
    from preview_gcode_image import gcode_to_preview_png  # type: ignore
    _HAS_RENDERER = True
except Exception:
    gcode_to_preview_png = None  # type: ignore

router = APIRouter(tags=["print-queue"])
logger = logging.getLogger("print_queue")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)

# =============================================================================
# Canonical Event Helpers (Holo-first, ใช้ทุกช่องทางจากไฟล์นี้ไฟล์เดียว)
# =============================================================================
_CANONICAL_TYPES = {
    "print.queued", "print.started", "print.completed",
    "print.failed", "print.canceled", "print.paused", "print.issue",
}

def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def _norm_event_type(original_type: Optional[str], status: Optional[str] = None) -> str:
    t = (original_type or "").strip().lower()
    s = (status or "").strip().lower()

    if t in _CANONICAL_TYPES:
        return t
    if t == "job-event" and s:
        if s == "cancelled":
            s = "canceled"
        if s in {"queued","started","completed","failed","canceled","paused","issue"}:
            return f"print.{s}"
    if t == "print_issue":
        return "print.issue"
    if t == "print.cancelled":
        return "print.canceled"
    return "print.issue"

def _norm_severity(t: str, sev_in: Optional[str]) -> str:
    base = (sev_in or "").strip().lower()
    if base in {"success","info","warning","error","critical","neutral"}:
        return base
    if t == "print.completed": return "success"
    if t == "print.failed":    return "error"
    if t == "print.paused":    return "warning"
    if t == "print.canceled":  return "neutral"
    if t in {"print.started", "print.queued"}: return "info"
    if t == "print.issue":     return "warning"
    return "info"

def _format_event(*,
    type: Optional[str] = None,
    status: Optional[str] = None,
    severity: Optional[str] = None,
    title: Optional[str] = None,
    message: Optional[str] = None,
    printer_id: Optional[str] = None,
    data: Optional[dict] = None,
    created_at: Optional[str] = None,
    read: Optional[bool] = False,
) -> dict:
    """
    ให้การ์ด Teams/เว็บมีข้อมูลครบและสอดคล้อง:
    - ใส่ status เป็น top-level เสมอ (queued/started/completed/failed/canceled/paused/issue)
    - sync data.status ด้วย
    """
    t = _norm_event_type(type, status)
    sev = _norm_severity(t, severity)

    canonical_status = (status or "").strip().lower()
    if not canonical_status:
        if t.startswith("print.") and len(t.split(".", 1)) == 2:
            canonical_status = t.split(".", 1)[1]
        else:
            canonical_status = "issue"

    default_titles = {
        "print.queued": "Queued",
        "print.started": "Print started",
        "print.completed": "Print completed",
        "print.failed": "Print failed",
        "print.canceled": "Print canceled",
        "print.paused": "Print paused",
        "print.issue": "Printer issue",
    }
    ttl = title or default_titles.get(t, "Notification")

    d = dict(data or {})
    if printer_id and not str(d.get("printer_id") or "").strip():
        d["printer_id"] = printer_id
    nm = (d.get("job_name") or d.get("name") or d.get("filename") or d.get("file") or "").strip()
    if nm:
        d.setdefault("name", nm)
        d.setdefault("job_name", nm)
    if not str(d.get("status") or "").strip():
        d["status"] = canonical_status

    if not message:
        prn = d.get("printer_id") or printer_id
        name_txt = f"“{nm}” " if nm else ""
        on_txt   = f" on {prn}" if prn else ""
        template = {
            "queued":    f"{name_txt}entered the queue{on_txt}.",
            "started":   f"{name_txt}is now starting{on_txt}.",
            "completed": f"{name_txt}finished{on_txt}.",
            "failed":    f"{name_txt}failed{on_txt}.",
            "canceled":  f"{name_txt}was canceled{on_txt}.",
            "paused":    f"{name_txt}has been paused{on_txt}.",
            "issue":     f"Issue detected {name_txt}{on_txt}.",
        }
        message = template.get(canonical_status, f"{name_txt}updated{on_txt}.")

    return {
        "id": str(uuid.uuid4()),
        "type": t,
        "status": canonical_status,            # สำคัญสำหรับ Teams
        "severity": sev,
        "title": ttl,
        "message": message,
        "printer_id": printer_id,
        "data": d,
        "created_at": created_at or _now_iso(),
        "read": bool(read),
    }

async def _call_notify_user_async(db: Session, employee_id: str, ev: dict):
    async def _run_kwargs():
        res = notify_user(db, employee_id, **ev)
        if inspect.iscoroutine(res):
            return await res
        return res
    async def _run_payload():
        res = notify_user(db, employee_id, payload=ev)
        if inspect.iscoroutine(res):
            return await res
        return res
    try:
        return await _run_kwargs()
    except TypeError:
        return await _run_payload()

async def _call_notify_job_event_async(db: Session, ev: dict):
    async def _run_kwargs():
        res = notify_job_event(db, ev, "canonical")
        if inspect.iscoroutine(res):
            return await res
        return res
    async def _run_payload():
        res = notify_job_event(db, payload=ev)
        if inspect.iscoroutine(res):
            return await res
        return res
    try:
        return await _run_kwargs()
    except TypeError:
        return await _run_payload()

def _emit_event_all_channels(db: Session, employee_id: str, ev: dict):
    try:
        _bgcall(_call_notify_user_async, db, employee_id, ev)
    except Exception:
        logger.exception("notify_user spawn failed")
    try:
        _bgcall(_call_notify_job_event_async, db, ev)
    except Exception:
        logger.exception("notify_job_event spawn failed")

# =============================================================================

def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() not in {"0", "false", "no", "off", ""}

ADMIN_TOKEN = (os.getenv("ADMIN_TOKEN") or "").strip()
DEFAULT_PRINTER_ID = os.getenv("DEFAULT_PRINTER_ID", "prusa-core-one")

BACKEND_INTERNAL_BASE = (
    os.getenv("MAIN_BACKEND")
    or os.getenv("BACKEND_INTERNAL_BASE")
    or "http://127.0.0.1:8001"
).rstrip("/")

PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").strip().strip('"').strip("'")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "Delta")

AUTO_START_ON_ENQUEUE    = _env_bool("AUTO_START_ON_ENQUEUE", True)
RESUME_DIRECT_PROCESSING = _env_bool("RESUME_DIRECT_PROCESSING", False)
ALLOW_ADMIN_HEADER       = _env_bool("ALLOW_ADMIN_HEADER", True)

# === Preview / PNG render params =============================================
AUTO_PREVIEW_ON_ENQUEUE = _env_bool("AUTO_PREVIEW_ON_ENQUEUE", True)
PREVIEW_HIDE_TRAVEL     = _env_bool("SLICER_PREVIEW_HIDE_TRAVEL", True)
PREVIEW_DPI             = int(os.getenv("SLICER_PREVIEW_DPI") or "500")
PREVIEW_LW              = float(os.getenv("SLICER_PREVIEW_LW") or "0.8")
PREVIEW_FADE            = float(os.getenv("SLICER_PREVIEW_FADE") or "0.7")
PREVIEW_ANTIALIAS       = _env_bool("SLICER_PREVIEW_AA", True)
MIN_OK_PREVIEW_BYTES    = int(os.getenv("MIN_OK_PREVIEW_BYTES") or "4096")
PREVIEW_DEBOUNCE_SEC    = float(os.getenv("PREVIEW_DEBOUNCE_SEC") or "300")
_PREVIEW_LOCKS: Dict[str, Lock] = {}
_PREVIEW_LAST_TS: Dict[str, float] = {}

QUEUE_IDEMP_TTL_SEC = float(os.getenv("QUEUE_IDEMP_TTL_SEC") or "12.0")
_IDEMP_LOCK = Lock()
_IDEMP_CACHE: Dict[str, Tuple[float, int]] = {}

def _clean_env(v: Optional[str]) -> str:
    return (v or "").strip().strip('"').strip("'")

OCTO_BASE = _clean_env(os.getenv("OCTOPRINT_BASE") or os.getenv("OCTOPRINT_BASE_URL") or "").rstrip("/")
OCTO_KEY  = _clean_env(os.getenv("OCTOPRINT_API_KEY") or "")
_timeout_raw = _clean_env(os.getenv("OCTOPRINT_HTTP_TIMEOUT") or os.getenv("OCTOPRINT_TIMEOUT") or "30")
try:
    _m = re.match(r"^\d+(\.\d+)?", _timeout_raw)
    OCTO_TIMEOUT = float(_m.group(0)) if _m else 30.0
except Exception:
    OCTO_TIMEOUT = 30.0

# ✅ เพิ่มแหล่งที่มาที่ใช้จริง (unity/catalog/user_history)
ALLOWED_SOURCE = {"upload", "history", "storage", "catalog", "user_history", "unity"}

# ==== Bed-empty gate ====
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except Exception:
        return int(default)

# ✅ ใช้ตัวเดียวให้ชัดเจน (ลบตัวแปรซ้ำก่อนหน้าออก)
REQUIRE_BED_EMPTY_FOR_PROCESS_NEXT = _env_bool("REQUIRE_BED_EMPTY_FOR_PROCESS_NEXT", True)
BED_EMPTY_MAX_AGE_SEC = _env_int("BED_EMPTY_MAX_AGE_SEC", 300)

async def _bed_empty_recent_async(printer_id: str) -> bool:
    """
    true เมื่อ /notifications/bed/status บอกว่าเห็น bed_empty ล่าสุด
    และอายุไม่เกิน BED_EMPTY_MAX_AGE_SEC วินาที
    """
    if not REQUIRE_BED_EMPTY_FOR_PROCESS_NEXT:
        return True
    if not ADMIN_TOKEN:
        logger.warning("REQUIRE_BED_EMPTY_FOR_PROCESS_NEXT=1 แต่ไม่มี ADMIN_TOKEN")
        return False
    url = f"{BACKEND_INTERNAL_BASE}/notifications/bed/status"
    params = {"printer_id": _norm_printer_id(printer_id)}
    headers = {"X-Admin-Token": ADMIN_TOKEN}
    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as c:
            r = await c.get(url, params=params, headers=headers)
            if r.status_code != 200:
                logger.info("[QUEUE] bed-status HTTP %s: %s", r.status_code, r.text[:200])
                return False
            js = r.json() or {}
            if not js.get("ok"):
                return False
            age = float(js.get("age_sec") or 9e9)
            return age <= float(BED_EMPTY_MAX_AGE_SEC)
    except Exception:
        logger.exception("[QUEUE] bed-status check failed")
        return False

def status_order_expr():
    return case(
        (PrintJob.status.in_(("processing", "printing")), 0),
        (PrintJob.status == "queued", 1),
        (PrintJob.status == "paused", 2),
        (PrintJob.status == "completed", 3),
        (PrintJob.status == "failed", 4),
        (PrintJob.status == "canceled", 5),
        else_=9,
    ).label("status_rank")

def _emp(x) -> str:
    return str(x or "").strip()

def _norm_printer_id(v: Optional[str]) -> str:
    s = (v or "").strip()
    s = re.sub(r"[^\w\s\-]+", "", s)
    s = re.sub(r"\s+", "-", s)
    return (s or DEFAULT_PRINTER_ID).lower()

def _get_or_create_printer(db: Session, pid: str) -> Printer:
    pid = _norm_printer_id(pid)
    p = db.query(Printer).filter(Printer.id == pid).first()
    if not p:
        p = Printer(
            id=pid,
            display_name=pid.replace("-", " ").title(),
            state="ready",
            status_text="Printer is ready",
        )
        db.add(p); db.commit(); db.refresh(p)
    return p

def _owner_or_manager(u: User, job: PrintJob) -> bool:
    return (_emp(u.employee_id) == _emp(job.employee_id)) or bool(getattr(u, "can_manage_queue", False))

def _can_cancel_with_reason(u: User, job: PrintJob) -> Tuple[bool, str]:
    if getattr(u, "can_manage_queue", False):
        if job.status in {"queued", "paused", "processing"}:
            return True, "manager"
        return False, f"status_not_cancelable:{job.status}"
    if _emp(u.employee_id) != _emp(job.employee_id):
        return False, "not_owner"
    if job.status not in {"queued", "paused"}:
        return False, f"status_not_cancelable:{job.status}"
    return True, "ok"

def _decorate_employee_name(db: Session, jobs: List[PrintJob]) -> Dict[str, str]:
    emp_ids = sorted({_emp(j.employee_id) for j in jobs if j.employee_id})
    if not emp_ids:
        return {}
    users = db.query(User).filter(User.employee_id.in_(emp_ids)).all()
    name_map = {_emp(u.employee_id): (u.name or _emp(u.employee_id)) for u in users}
    for eid in emp_ids:
        name_map.setdefault(eid, eid)
    return name_map

# ✅ ช่วย map thumb (object key) → URL ใช้งานได้จริง
_HTTP_RE = re.compile(r"^https?://", re.I)
def _thumb_to_url(val: Optional[str]) -> Optional[str]:
    s = (val or "").strip()
    if not s:
        return None
    if _HTTP_RE.match(s):
        return s
    # เดาว่าเป็น object key ใน MinIO → ให้โหลดผ่าน proxy /files/raw
    return f"/files/raw?object_key={quote(s, safe='')}"

def _to_out(db: Session, current_user: User, job: PrintJob, name_map: Optional[Dict[str, str]] = None) -> PrintJobOut:
    j_source = (getattr(job, "source", None) or "").strip().lower()
    if j_source not in ALLOWED_SOURCE:
        j_source = "storage"
    try:
        o = PrintJobOut.model_validate(job, from_attributes=True)
    except Exception:
        payload = {k: getattr(job, k, None) for k in [
            "id","printer_id","employee_id","name","thumb","time_min","status",
            "uploaded_at","started_at","finished_at","octoprint_job_id"
        ]}
        payload["source"] = j_source
        o = PrintJobOut.model_validate(payload)

    # ✅ ให้ FE ยืนยันยกเลิกได้เฉพาะของตัวเอง/manager
    ok, _ = _can_cancel_with_reason(current_user, job)
    if hasattr(o, "me_can_cancel"):
        o.me_can_cancel = ok

    # ✅ แปลง thumb เป็น URL ทุกครั้ง (งานจาก Unity/Storage จะขึ้นรูปเหมือนเว็บ)
    try:
        if hasattr(o, "thumb") and getattr(o, "thumb", None):
            o.thumb = _thumb_to_url(o.thumb)  # type: ignore[attr-defined]
    except Exception:
        pass

    # เติมชื่อพนักงานถ้ามีสคีมา
    if hasattr(o, "employee_name"):
        if name_map is not None:
            o.employee_name = name_map.get(_emp(job.employee_id), _emp(job.employee_id))
        else:
            usr = db.query(User).filter(User.employee_id == _emp(job.employee_id)).first()
            o.employee_name = (usr.name if usr and usr.name else _emp(job.employee_id))
    return o

def _guess_ct(name_or_key: str, default: str = "application/octet-stream") -> str:
    ct, _ = mimetypes.guess_type(name_or_key or "")
    lower = str(name_or_key or "").lower()
    if not ct and lower.endswith((".gcode", ".gco", ".gc")):
        ct = "text/x.gcode"
    if not ct and lower.endswith(".stl"):
        ct = "model/stl"
    return ct or default

def _is_gcode_name(name_or_key: str) -> bool:
    lower = (name_or_key or "").lower()
    return lower.endswith((".gcode", ".gco", ".gc"))

def _is_stl_name(name_or_key: str) -> bool:
    return (name_or_key or "").lower().endswith(".stl")

def _resolve_owner_by_gkey(db: Session, gcode_key_or_path: Optional[str], default_emp: str) -> str:
    k = (gcode_key_or_path or "").strip()
    if not k or not k.startswith(("storage/", "catalog/")):
        return default_emp
    row = db.query(StorageFile).filter(StorageFile.object_key == k).first()
    return _emp(row.employee_id) if row and row.employee_id else default_emp

# ----------------------- Background submit helper ----------------------------
async def _run_async(fn: Callable[..., Any], *args, **kwargs):
    await fn(*args, **kwargs)

def _submit_bg(tasks: Optional[BackgroundTasks], fn: Callable[..., Any], *args, **kwargs) -> None:
    if tasks is not None:
        if inspect.iscoroutinefunction(fn):
            tasks.add_task(_run_async, fn, *args, **kwargs)
        else:
            tasks.add_task(fn, *args, **kwargs)
        return
    try:
        loop = asyncio.get_running_loop()
        if inspect.iscoroutinefunction(fn):
            loop.create_task(fn(*args, **kwargs))
        else:
            loop.run_in_executor(None, lambda: fn(*args, **kwargs))
    except RuntimeError:
        _bgcall(fn, *args, **kwargs)

# --------------------------- storage helpers --------------------------------
def _ensure_storage_record(
    db: Session,
    employee_id: str,
    object_key: str,
    filename_hint: Optional[str] = None,
) -> None:
    if not object_key or not object_key.startswith(("storage/", "catalog/")):
        return
    exists = (
        db.query(StorageFile)
        .filter(
            StorageFile.employee_id == _emp(employee_id),
            StorageFile.object_key == object_key,
        )
        .first()
    )
    if exists:
        return
    base = os.path.basename(object_key)
    ct = _guess_ct(base)
    size = None
    try:
        h = head_object(object_key)
        size = int(h.get("ContentLength", 0) or h.get("Content-Length", "0") or 0)
        ct = (h.get("ContentType") or h.get("Content-Type") or ct)
    except Exception:
        pass
    row = StorageFile(
        employee_id=_emp(employee_id),
        filename=base,
        name="",
        object_key=object_key,
        content_type=ct,
        size=size,
        uploaded_at=datetime.utcnow(),
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        exists2 = (
            db.query(StorageFile)
            .filter(
                StorageFile.employee_id == _emp(employee_id),
                StorageFile.object_key == object_key,
            )
            .first()
        )
        if exists2:
            return
        return

def _ingest_uploads_to_storage(src_path: str, dst_name: str) -> str:
    if not PUBLIC_BASE_URL:
        raise HTTPException(500, "PUBLIC_BASE_URL_not_set")
    if put_object is None:
        raise HTTPException(500, "s3util.put_object_not_available")
    url = f"{PUBLIC_BASE_URL}{src_path}"
    dst_key = new_storage_key(dst_name)
    try:
        with httpx.Client(timeout=OCTO_TIMEOUT, follow_redirects=True) as c:
            r = c.get(url); r.raise_for_status()
            data = r.content
    except Exception as e:
        raise HTTPException(500, f"read_uploads_failed:{src_path}") from e
    try:
        put_object(dst_key, data, _guess_ct(dst_name))  # type: ignore
    except Exception as e:
        raise HTTPException(500, f"storage_put_failed:{dst_key}") from e
    return dst_key

def _derive_model_for_finalize(printer_id: str, payload: Optional[PrintJobCreate]) -> str:
    try:
        if payload and isinstance(payload.template, dict):
            m = payload.template.get("model")
            if isinstance(m, str) and m.strip():
                return m.strip()
    except Exception:
        pass
    return DEFAULT_MODEL

def _finalize_object_if_staging(
    db: Session,
    employee_id: str,
    src_key: Optional[str],
    display_name: Optional[str] = None,
    want_record: bool = True,
    *,
    model_for_catalog: Optional[str] = None,
    user: Optional[User] = None,
) -> Optional[str]:
    if not src_key:
        return src_key
    if src_key.startswith(("storage/", "catalog/")):
        if want_record:
            _ensure_storage_record(db, employee_id, src_key, None)
        return src_key
    if src_key.startswith("/uploads/"):
        src_base = os.path.basename(src_key)
        if _is_gcode_name(src_base):
            dst_key = _ingest_uploads_to_storage(src_key, src_base)
            if want_record:
                _ensure_storage_record(db, employee_id, dst_key, None)
            return dst_key
        return src_key
    if src_key.startswith("staging/"):
        src_base = os.path.basename(src_key)
        if not (_is_gcode_name(src_base) or _is_stl_name(src_base)):
            logger.info("Skip finalize unknown type from staging: %s", src_key)
            return src_key
        if finalize_to_storage is None:
            dst_key = new_storage_key(src_base)
            ct = _guess_ct(src_base)
            try:
                copy_object(src_key, dst_key, content_type=ct)
            except Exception as e:
                raise HTTPException(500, f"storage_copy_failed:{src_key}") from e
            if want_record:
                _ensure_storage_record(db, employee_id, dst_key, None)
            try:
                delete_object(src_key)
            except Exception:
                pass
            return dst_key
        fin = FinalizeIn(
            object_key=src_key,
            filename=src_base,
            model=(model_for_catalog or DEFAULT_MODEL),
            target="catalog",
        )
        try:
            out = finalize_to_storage(fin, db=db, me=user)  # type: ignore
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"finalize_failed:{e}")
        return out.object_key
    return src_key

# --------------------------- preview helpers ---------------------------------
def _preview_key_for(gcode_key: Optional[str]) -> Optional[str]:
    if not gcode_key:
        return None
    return re.sub(r"\.(gcode|gco|gc)$", ".preview.png", gcode_key, flags=re.I)

def _download_to_temp(object_key: str) -> Tuple[str, bytes]:
    url = presign_get(object_key)
    with httpx.Client(timeout=OCTO_TIMEOUT, follow_redirects=True) as c:
        r = c.get(url); r.raise_for_status()
        return object_key, r.content

def _auto_render_preview(gcode_key: str) -> Optional[str]:
    if not (_HAS_RENDERER and put_object and gcode_key and _is_gcode_name(gcode_key)):
        return None
    try:
        import tempfile
        _, g_bytes = _download_to_temp(gcode_key)
        with tempfile.TemporaryDirectory() as td:
            gpath = os.path.join(td, "src.gcode")
            png   = os.path.join(td, "out.png")
            with open(gpath, "wb") as f:
                f.write(g_bytes)
            gcode_to_preview_png(  # type: ignore
                gpath, png,
                include_travel=(not PREVIEW_HIDE_TRAVEL),
                lw=PREVIEW_LW,
                fade=PREVIEW_FADE,
                dpi=PREVIEW_DPI,
                antialias=PREVIEW_ANTIALIAS,
            )
            with open(png, "rb") as pf:
                preview_key = _preview_key_for(gcode_key)
                if not preview_key:
                    return None
                put_object(preview_key, pf.read(), "image/png")  # type: ignore
                logger.info("Preview rendered: %s", preview_key)
                return preview_key
    except Exception:
        logger.exception("auto-render preview failed for %s", gcode_key)
        return None

def ensure_preview_once(gcode_key: str) -> Optional[str]:
    preview_key = _preview_key_for(gcode_key)
    if not preview_key:
        return None
    try:
        h = head_object(preview_key)
        size = int(h.get("Content-Length", "0") or h.get("ContentLength", 0) or 0)
        if size >= MIN_OK_PREVIEW_BYTES:
            _PREVIEW_LAST_TS[preview_key] = time.time()
            return preview_key
    except Exception:
        pass
    last = _PREVIEW_LAST_TS.get(preview_key, 0.0)
    if (time.time() - last) < PREVIEW_DEBOUNCE_SEC:
        return preview_key
    lock = _PREVIEW_LOCKS.setdefault(preview_key, Lock())
    acquired = lock.acquire(blocking=False)
    if not acquired:
        return preview_key
    try:
        try:
            h2 = head_object(preview_key)
            size2 = int(h2.get("Content-Length", "0") or h2.get("ContentLength", 0) or 0)
            if size2 >= MIN_OK_PREVIEW_BYTES:
                _PREVIEW_LAST_TS[preview_key] = time.time()
                return preview_key
        except Exception:
            pass
        res = _auto_render_preview(gcode_key)
        if res:
            _PREVIEW_LAST_TS[preview_key] = time.time()
        return res or preview_key
    finally:
        try:
            lock.release()
        except Exception:
            pass

# --------------------------- Idempotent enqueue ------------------------------
def _make_idem_key(printer_id: str, employee_id: str, gcode_key: Optional[str], explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit.strip()
    if not gcode_key:
        return None
    return f"enqueue:{_norm_printer_id(printer_id)}:{_emp(employee_id)}:{gcode_key}"

def _find_recent_duplicate_job(
    db: Session,
    printer_id: str,
    employee_id: str,
    gcode_key_or_path: Optional[str],
) -> Optional[PrintJob]:
    if not gcode_key_or_path:
        return None
    cutoff = datetime.utcnow() - timedelta(seconds=QUEUE_IDEMP_TTL_SEC)
    return (
        db.query(PrintJob)
        .filter(
            PrintJob.printer_id == _norm_printer_id(printer_id),
            PrintJob.employee_id == _emp(employee_id),
            PrintJob.gcode_path == gcode_key_or_path,
            PrintJob.uploaded_at >= cutoff,
            PrintJob.status.in_(("queued", "processing", "paused")),
        )
        .order_by(PrintJob.uploaded_at.desc(), PrintJob.id.desc())
        .first()
    )

def _get_cached_idem_job(db: Session, key: Optional[str]) -> Optional[PrintJob]:
    if not key:
        return None
    with _IDEMP_LOCK:
        rec = _IDEMP_CACHE.get(key)
        if not rec:
            return None
        ts, jid = rec
        if time.time() - ts > QUEUE_IDEMP_TTL_SEC:
            _IDEMP_CACHE.pop(key, None)
            return None
    job = db.query(PrintJob).filter(PrintJob.id == jid).first()
    if job and job.status in {"queued", "processing", "paused"}:
        return job
    return None

def _cache_idem_job(key: Optional[str], job_id: int) -> None:
    if not key:
        return
    with _IDEMP_LOCK:
        _IDEMP_CACHE[key] = (time.time(), job_id)

# --------------------------- OctoPrint settings/ops --------------------------
def _octo_headers() -> dict:
    if OCTO_KEY:
        return {"X-Api-Key": OCTO_KEY, "User-Agent": "ADI-3DP-Backend/Queue"}
    return {"User-Agent": "ADI-3DP-Backend/Queue"}

def _safe_filename(name: str) -> str:
    n = re.sub(r"[^\w.\-]+", "_", name or "job.gcode")
    if not n.lower().endswith(".gcode"):
        n += ".gcode"
    return n

async def _download_bytes(src: str) -> bytes:
    async with httpx.AsyncClient(timeout=OCTO_TIMEOUT, follow_redirects=True) as client:
        s = (src or "").strip()
        if not s:
            raise RuntimeError("empty_source")
        if s.startswith(("storage/", "catalog/", "staging/", "printer-store/")):
            url = presign_get(s)
            r = await client.get(url); r.raise_for_status()
            return r.content
        if s.startswith("/uploads/"):
            if not PUBLIC_BASE_URL:
                raise RuntimeError("PUBLIC_BASE_URL_not_set")
            url = f"{PUBLIC_BASE_URL}{s}"
            r = await client.get(url); r.raise_for_status()
            return r.content
        if s.startswith(("http://", "https://")):
            r = await client.get(s); r.raise_for_status()
            return r.content
        if os.path.exists(s):
            with open(s, "rb") as f:
                return f.read()
        try:
            url = presign_get(s)
            r = await client.get(url); r.raise_for_status()
            return r.content
        except Exception:
            pass
        raise RuntimeError(f"unsupported_source:{src}")

def _octo_is_ready() -> bool:
    if not (OCTO_BASE and OCTO_KEY):
        # ไม่มี config → ถือว่า “พร้อม” เพื่อให้ flow dev ไปต่อได้
        return True
    url = f"{OCTO_BASE}/api/printer"
    try:
        with httpx.Client(timeout=5.0, follow_redirects=True) as c:
            r = c.get(url, headers=_octo_headers())
            if r.status_code >= 300:
                return False
            js = r.json() or {}
            flags = ((js.get("state") or {}).get("flags") or {})
            busy = any(bool(flags.get(k)) for k in ("printing", "paused", "pausing", "cancelling"))
            return not busy
    except Exception:
        return False

async def _dispatch_to_octoprint(db: Session, job: PrintJob, tasks: Optional[BackgroundTasks] = None) -> None:
    if not (OCTO_BASE and OCTO_KEY):
        logger.warning("OctoPrint not configured (base/key missing), skip dispatch")
        return

    src = (getattr(job, "gcode_path", "") or getattr(job, "gcode_key", "") or "").strip()
    if not src:
        logger.error("Job %s has no gcode source (gcode_path/key is empty)", job.id)
        return

    filename = _safe_filename(os.path.basename(src) or f"job_{job.id}.gcode")
    try:
        file_bytes = await _download_bytes(src)
    except Exception:
        logger.exception("Download gcode failed for job %s", job.id)
        job.status = "failed"
        job.finished_at = datetime.utcnow()
        db.add(job); db.commit(); db.refresh(job)
        evf = _format_event(
            type="print.failed",
            printer_id=job.printer_id,
            data={"name": job.name, "job_id": int(job.id), "reason": "download_failed"},
        )
        _submit_bg(tasks, _emit_event_all_channels, db, job.employee_id, evf)
        return

    files = {"file": (filename, file_bytes, _guess_ct(filename))}
    qs = urlencode({"select": "true", "print": "true"})
    url = f"{OCTO_BASE}/api/files/local?{qs}"

    delays = [0.0, 2.0, 4.0, 8.0]
    last_err = None

    for attempt, delay in enumerate(delays, start=1):
        if delay:
            await asyncio.sleep(delay)

        if not _octo_is_ready():
            logger.info("Octo not ready (attempt %s), waiting 2s", attempt)
            await asyncio.sleep(2.0)

        try:
            async with httpx.AsyncClient(timeout=OCTO_TIMEOUT) as client2:
                up = await client2.post(url, headers=_octo_headers(), files=files)
            sc = up.status_code
            if sc < 300:
                logger.info("OctoPrint: uploaded & started %s (attempt %s)", filename, attempt)
                evs = _format_event(
                    type="print.started",
                    printer_id=job.printer_id,
                    data={"name": job.name, "job_id": int(job.id)},
                )
                _submit_bg(tasks, _emit_event_all_channels, db, job.employee_id, evs)
                return

            body = (up.text or "")[:300]
            logger.warning("Octo upload attempt %s failed %s: %s", attempt, sc, body)
            if sc in (409, 423, 429) or (500 <= sc < 600):
                last_err = RuntimeError(f"octoprint_upload_failed:{sc}")
                continue
            up.raise_for_status()

        except Exception as e:
            last_err = e
            logger.exception("Octo push exception (attempt %s): %s", attempt, e)

    if last_err:
        logger.error("OctoPrint push failed after retries for job %s", job.id)
        job.status = "failed"
        job.finished_at = datetime.utcnow()
        db.add(job); db.commit(); db.refresh(job)
        evf = _format_event(
            type="print.failed",
            printer_id=job.printer_id,
            data={"name": job.name, "job_id": int(job.id), "reason": "octoprint_unreachable"},
        )
        _submit_bg(tasks, _emit_event_all_channels, db, job.employee_id, evf)
        return

# -------------------------- RUNMAP binder ------------------------------------
def _bind_runmap_remote(printer_id: str, job: PrintJob, *, octo_user: str | None = None) -> None:
    try:
        try:
            from printer_status import _bind_runmap as _bind_runmap_core  # type: ignore
        except Exception:
            from backend.printer_status import _bind_runmap as _bind_runmap_core  # type: ignore
        _bind_runmap_core(
            printer_id,
            job_id=int(job.id),
            employee_id=_emp(job.employee_id),
            name=job.name or "",
            octo_user=octo_user or "",
        )
        logger.info("[RUNMAP] bind local ok job#%s", job.id)
        return
    except Exception:
        logger.exception("[RUNMAP] bind local failed, fallback HTTP")

    if not ADMIN_TOKEN:
        return
    url = f"{BACKEND_INTERNAL_BASE}/printers/{_norm_printer_id(printer_id)}/internal/runmap/bind"
    headers = {"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json"}
    payload = {
        "job_id": int(job.id),
        "employee_id": _emp(job.employee_id),
        "name": job.name or "",
        "octo_user": octo_user or "",
    }
    try:
        timeout = httpx.Timeout(5.0, connect=2.0, read=2.0, write=2.0)
        with httpx.Client(timeout=timeout, follow_redirects=True) as c:
            r = c.post(url, headers=headers, json=payload)
            logger.info("[RUNMAP] bind HTTP → %s %s", r.status_code, r.text[:200])
    except Exception:
        logger.exception("[RUNMAP] bind HTTP failed")

# -------------------------- notifier (async-first) ---------------------------
def _notify_job_event_async(job_id: int, status_out: str, printer_id: str, name: str | None):
    async def _run():
        try:
            try:
                from printer_status import _notify_job_event as _notify_local  # type: ignore
            except Exception:
                from backend.printer_status import _notify_job_event as _notify_local  # type: ignore
            asyncio.create_task(_notify_local(job_id, status_out, printer_id=printer_id, name=name))
            logger.info("[QUEUE] notify job-event (local) %s #%s", status_out, job_id)
            return
        except Exception:
            logger.exception("[QUEUE] notify local not available, fallback HTTP]")
        try:
            base = BACKEND_INTERNAL_BASE
            admin = ADMIN_TOKEN
            url = f"{base}/notifications/job-event"
            headers = {"X-Admin-Token": admin, "Content-Type": "application/json"}
            payload = {"job_id": job_id, "status": status_out, "printer_id": printer_id, "name": name}
            timeout = httpx.Timeout(5.0, connect=2.0, read=2.0, write=2.0)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
                r = await c.post(url, json=payload, headers=headers)
                logger.info("[QUEUE] notify job-event (HTTP) %s → %s", status_out, r.status_code)
        except Exception:
            logger.exception("[QUEUE] notify job-event (HTTP) failed")

    _bgcall(_run)

# -------------------------- bed-empty status (NEW) ---------------------------
def _bed_empty_recent_sync(printer_id: str) -> bool:
    """
    ดึงสถานะเตียงล่าสุดจาก notifications service
    ยอมให้ผ่านเมื่อ:
      - ปิด REQUIRE_BED_EMPTY_FOR_PROCESS_NEXT หรือ
      - age_sec <= BED_EMPTY_MAX_AGE_SEC
    """
    if not REQUIRE_BED_EMPTY_FOR_PROCESS_NEXT:  # ✅ ใช้ชื่อตัวแปรเดียว
        return True
    if not ADMIN_TOKEN:
        logger.warning("[QUEUE] bed-gate: ADMIN_TOKEN missing -> block")
        return False
    url = f"{BACKEND_INTERNAL_BASE}/notifications/bed/status"
    try:
        with httpx.Client(timeout=5.0, follow_redirects=True) as c:
            r = c.get(url, params={"printer_id": printer_id}, headers={"X-Admin-Token": ADMIN_TOKEN})
            if r.status_code != 200:
                logger.info("[QUEUE] bed-gate: HTTP %s from %s", r.status_code, url)
                return False
            js = r.json() or {}
            if not js.get("ok"):
                logger.info("[QUEUE] bed-gate: no ok (%s)", js.get("reason"))
                return False
            age = float(js.get("age_sec") or 1e9)
            ok = (age <= BED_EMPTY_MAX_AGE_SEC)
            if not ok:
                logger.info("[QUEUE] bed-gate: age %.1fs > limit %ss", age, BED_EMPTY_MAX_AGE_SEC)
            return ok
    except Exception:
        logger.exception("[QUEUE] bed-gate: check failed")
        return False

# -------------------------- queue flow helpers -------------------------------
def _start_next_job_if_idle(db: Session, printer_id: str, tasks: Optional[BackgroundTasks] = None) -> Optional[PrintJob]:
    printer_id = _norm_printer_id(printer_id)

    # ยังมีงานกำลังวิ่งอยู่ → ไม่เริ่ม
    has_processing = db.query(PrintJob).filter(
        PrintJob.printer_id == printer_id,
        PrintJob.status == "processing"
    ).first()
    if has_processing:
        return None

    # ไม่มีคิว → จบ
    next_job = db.query(PrintJob).filter(
        PrintJob.printer_id == printer_id,
        PrintJob.status == "queued"
    ).order_by(PrintJob.uploaded_at.asc(), PrintJob.id.asc()).first()
    if not next_job:
        return None

    # === NEW: bed-empty gate ===
    if REQUIRE_BED_EMPTY_FOR_PROCESS_NEXT:
        ok = _bed_empty_recent_sync(printer_id)
        if not ok:
            logger.info("[QUEUE] block start: bed not confirmed empty (printer=%s, job#%s)", printer_id, next_job.id)
            return None

    # ผ่าน gate → เริ่มงาน
    now = datetime.utcnow()
    next_job.status = "processing"
    if not next_job.started_at:
        next_job.started_at = now
    db.add(next_job); db.commit(); db.refresh(next_job)

    _bind_runmap_remote(printer_id, next_job)
    _submit_bg(tasks, _dispatch_to_octoprint, db, next_job, tasks)
    return next_job

# -------------------------- time computation ---------------------------------
def _compute_times(rows: List[PrintJob]) -> Dict[int, Tuple[int, int, int]]:
    now = datetime.utcnow()
    result: Dict[int, Tuple[int, int, int]] = {}
    cumulative = 0
    for j in rows:
        base = int(j.time_min or 0)
        if j.status == "processing" and j.started_at and base > 0:
            elapsed = max(int((now - j.started_at).total_seconds() // 60), 0)
            remaining = max(base - elapsed, 0)
        else:
            remaining = base
        wait_before = cumulative
        wait_total = wait_before + remaining
        result[j.id] = (wait_before, wait_total, remaining)
        cumulative += remaining
    return result

# -----------------------------------------------------------------------------#
# core enqueue
# -----------------------------------------------------------------------------#
def _enqueue_job(
    db: Session,
    current: User,
    payload: PrintJobCreate,
    printer_id: str,
    tasks: Optional[BackgroundTasks] = None,
    *,
    idempotency_key: Optional[str] = None,
) -> PrintJobOut:
    printer_id = _norm_printer_id(printer_id)
    _get_or_create_printer(db, printer_id)

    original_key_in = getattr(payload, "original_key", None)
    gcode_key_in  = getattr(payload, "gcode_key", None)
    gcode_path_in = getattr(payload, "gcode_path", None)
    name = payload.name

    model_for_catalog = _derive_model_for_finalize(printer_id, payload)

    gcode_src_in = gcode_path_in or gcode_key_in
    same_key = bool(original_key_in and gcode_src_in and original_key_in == gcode_src_in)

    if original_key_in and not same_key:
        _ = _finalize_object_if_staging(
            db, _emp(current.employee_id),
            original_key_in,
            display_name=name or original_key_in,
            want_record=False,
            model_for_catalog=model_for_catalog,
            user=current,
        )

    gcode_final = None
    if gcode_src_in:
        gcode_final = _finalize_object_if_staging(
            db, _emp(current.employee_id),
            gcode_src_in,
            display_name=name or gcode_src_in,
            want_record=True,
            model_for_catalog=model_for_catalog,
            user=current,
        )

    db.commit()

    gk = (gcode_final or gcode_path_in or gcode_key_in)
    pkey = _preview_key_for(gk) if gk else None
    job_thumb = payload.thumb or pkey  # ✅ เซ็ตเป็น object key ไปก่อน เดี๋ยวตอนส่งออก map เป็น URL ให้

    if AUTO_PREVIEW_ON_ENQUEUE and gk:
        def _bg_render():
            try:
                ensure_preview_once(gk)
            except Exception:
                logger.exception("background preview render failed for %s", gk)
        _submit_bg(tasks, _bg_render)

    owner_emp = _resolve_owner_by_gkey(db, gk, _emp(current.employee_id))
    requester_emp = _emp(current.employee_id)

    idem_key = _make_idem_key(printer_id, owner_emp, gk, idempotency_key)

    cached = _get_cached_idem_job(db, idem_key)
    if cached:
        logger.info("enqueue idempotent-hit (cache): printer=%s emp=%s gk=%s -> job_id=%s",
                    printer_id, owner_emp, gk, cached.id)
        return _to_out(db, current, cached)

    dup = _find_recent_duplicate_job(db, printer_id, owner_emp, gk)
    if dup:
        _cache_idem_job(idem_key, dup.id)
        logger.info("enqueue idempotent-hit (db): printer=%s emp=%s gk=%s -> job_id=%s",
            printer_id, owner_emp, gk, dup.id)
        return _to_out(db, current, dup)

    job_kwargs = dict(
        printer_id=printer_id,
        employee_id=owner_emp,
        name=name,
        thumb=job_thumb,
        time_min=payload.time_min,
        source=(payload.source or "storage"),
        gcode_path=gk,
        status="queued",
        uploaded_at=datetime.utcnow(),
    )
    if hasattr(PrintJob, "requested_by_employee_id"):
        job_kwargs["requested_by_employee_id"] = requester_emp

    job = PrintJob(**job_kwargs)

    try:
        if hasattr(job, "template_json") and getattr(payload, "template", None) is not None:
            job.template_json = json.dumps(payload.template, ensure_ascii=False)
    except Exception:
        logger.exception("serialize template_json failed")
    try:
        if hasattr(job, "stats_json") and getattr(payload, "stats", None) is not None:
            job.stats_json = json.dumps(payload.stats, ensure_ascii=False)
    except Exception:
        logger.exception("serialize stats_json failed")
    try:
        if hasattr(job, "file_json") and getattr(payload, "file", None) is not None:
            job.file_json = json.dumps(payload.file, ensure_ascii=False)
    except Exception:
        logger.exception("serialize file_json failed")

    db.add(job); db.commit(); db.refresh(job)
    _cache_idem_job(idem_key, job.id)

    evq = _format_event(
        type="print.queued",
        printer_id=printer_id,
        data={"name": job.name, "job_id": int(job.id)},
    )
    _submit_bg(tasks, _emit_event_all_channels, db, job.employee_id, evq)

    if AUTO_START_ON_ENQUEUE:
        _start_next_job_if_idle(db, printer_id, tasks)

    db.refresh(job)
    return _to_out(db, current, job)

# -----------------------------------------------------------------------------#
# create
# -----------------------------------------------------------------------------#
@router.post("/api/print", response_model=PrintJobOut)
def create_print(
    payload: PrintJobCreate,
    background_tasks: BackgroundTasks,
    printer_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current: User = Depends(get_confirmed_user),
    idempotency_key: Optional[str] = Header(None),
):
    pid = _norm_printer_id(printer_id or DEFAULT_PRINTER_ID)
    return _enqueue_job(db, current, payload, pid, background_tasks, idempotency_key=idempotency_key)

@router.post("/printers/{printer_id}/queue", response_model=PrintJobOut)
def enqueue_for_printer(
    printer_id: str,
    payload: PrintJobCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current: User = Depends(get_confirmed_user),
    idempotency_key: Optional[str] = Header(None),
):
    pid = _norm_printer_id(printer_id)
    return _enqueue_job(db, current, payload, pid, background_tasks, idempotency_key=idempotency_key)

# -----------------------------------------------------------------------------#
# list (รองรับ optional user + X-Admin-Token)
# -----------------------------------------------------------------------------#
@router.get("/printers/{printer_id}/queue", response_model=QueueListOut)
def list_queue(
    printer_id: str,
    include_all: bool = True,
    db: Session = Depends(get_db),
    current: Optional[User] = Depends(get_optional_user),
    x_admin_token: str = Header(default=""),
):
    is_admin = bool(ALLOW_ADMIN_HEADER and ADMIN_TOKEN and x_admin_token and x_admin_token == ADMIN_TOKEN)
    if not (current or is_admin):
        raise HTTPException(401, "Not authenticated")

    if not current and is_admin:
        class _U:
            employee_id = "admin"
            can_manage_queue = True
            confirmed = True
            name = "Admin"
        current = _U()  # type: ignore

    pid = _norm_printer_id(printer_id)
    q = db.query(PrintJob).filter(PrintJob.printer_id == pid)
    if not include_all:
        q = q.filter(PrintJob.status.in_(("queued", "processing", "paused", "printing")))

    rows: List[PrintJob] = q.order_by(status_order_expr(), PrintJob.uploaded_at.asc(), PrintJob.id.asc()).all()
    times = _compute_times(rows)
    name_map = _decorate_employee_name(db, rows)

    items: List[PrintJobOut] = []
    for j in rows:
        o = _to_out(db, current, j, name_map=name_map)  # type: ignore[arg-type]
        wb, wt, rem = times.get(j.id, (0, (o.time_min or 0), (o.time_min or 0)))
        o.wait_before_min = wb
        o.wait_total_min  = wt
        o.remaining_min   = rem
        items.append(o)
    return QueueListOut(printer_id=pid, items=items)

# -----------------------------------------------------------------------------#
# current job (มี fallback ไปสอบถาม OctoPrint)
# -----------------------------------------------------------------------------#
@router.get("/api/printers/{printer_id}/current-job", response_model=CurrentJobOut)
def current_job_for_printer(
    printer_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    pid = _norm_printer_id(printer_id)
    rows: List[PrintJob] = (
        db.query(PrintJob)
          .filter(PrintJob.printer_id == pid)
          .order_by(status_order_expr(), PrintJob.uploaded_at.asc(), PrintJob.id.asc())
          .all()
    )
    if rows:
        cur = next((r for r in rows if r.status == "processing"), None) \
              or next((r for r in rows if r.status == "queued"), None)
        if cur:
            qnum = rows.index(cur) + 1
            remaining = None
            if cur.time_min is not None:
                base = int(cur.time_min or 0)
                if cur.status == "processing" and cur.started_at and base > 0:
                    elapsed = max(int((datetime.utcnow() - cur.started_at).total_seconds() // 60), 0)
                    remaining = max(base - elapsed, 0)
                else:
                    remaining = base
            return CurrentJobOut(
                queue_number=qnum,
                file_name=cur.name or "(Unknown)",
                thumbnail_url=_thumb_to_url(cur.thumb) or "/images/placeholder-model.png",  # ✅ map เป็น URL เสมอ
                job_id=cur.id,
                status=("processing" if cur.status == "processing" else cur.status),
                started_at=cur.started_at,
                time_min=cur.time_min,
                remaining_min=remaining,
            )

    try:
        with httpx.Client(timeout=6.0) as c:
            r = c.get(f"{BACKEND_INTERNAL_BASE}/printers/{pid}/octoprint/job", params={"force": "true"})
        if r.status_code == 200:
            js = r.json()
            m = (js.get("mapped") or {})
            state = (m.get("state") or "").lower()
            if state == "printing":
                file_name = m.get("file_name") or m.get("file")
                if not file_name:
                    try:
                        file = (((js.get("octoprint") or {}).get("job") or {}).get("file") or {})
                        file_name = file.get("display") or file.get("name") or "(Printing)"
                    except Exception:
                        file_name = "(Printing)"
                sec_left = m.get("time_left") or m.get("timeLeft") or 0
                try:
                    remaining_min = max(int(float(sec_left) // 60), 0)
                except Exception:
                    remaining_min = None
                return CurrentJobOut(
                    queue_number=1,
                    file_name=file_name,
                    thumbnail_url="/images/placeholder-model.png",
                    job_id=0,
                    status="processing",
                    started_at=None,
                    time_min=None,
                    remaining_min=remaining_min,
                )
    except Exception:
        pass
    raise HTTPException(404, "No active job")

# -----------------------------------------------------------------------------#
# patch (rename / change status)
# -----------------------------------------------------------------------------#
@router.patch("/printers/jobs/{job_id}", response_model=PrintJobOut)
def patch_job(
    job_id: int,
    payload: PrintJobPatch,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current: User = Depends(get_confirmed_user),
):
    job = db.query(PrintJob).filter(PrintJob.id == job_id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    if not _owner_or_manager(current, job):
        raise HTTPException(403, "Forbidden")

    requested_status = (payload.status or "").strip().lower() if payload.status else None
    if payload.name is not None and requested_status != "processing":
        job.name = payload.name

    if requested_status:
        s = requested_status
        now = datetime.utcnow()

        if s == "processing":
            if job.started_at is None:
                job.started_at = now
            _bind_runmap_remote(job.printer_id, job)
            # ยิง started เมื่อ upload สำเร็จใน _dispatch_to_octoprint

        if s in ("completed", "failed", "canceled"):
            job.finished_at = now

        job.status = s

        if s in ("completed", "failed", "canceled"):
            status_out = "cancelled" if s == "canceled" else s
            _notify_job_event_async(job.id, status_out, job.printer_id, job.name)

            ev2 = _format_event(
                type=f"print.{ 'canceled' if s=='canceled' else s }",
                printer_id=job.printer_id,
                data={"name": job.name, "job_id": int(job.id)},
            )
            _submit_bg(background_tasks, _emit_event_all_channels, db, job.employee_id, ev2)

    db.add(job); db.commit(); db.refresh(job)
    return _to_out(db, current, job)

# -----------------------------------------------------------------------------#
# cancel (รวมจุดใช้งาน)
# -----------------------------------------------------------------------------#
def _poll_octoprint_ready_and_chain(db: Session, printer_id: str, max_wait_sec: float = 30.0, interval: float = 2.0):
    t0 = time.time()
    while (time.time() - t0) < max_wait_sec:
        if _octo_is_ready():
            _start_next_job_if_idle(db, printer_id, None)
            return
        try:
            time.sleep(interval)
        except Exception:
            break
    _start_next_job_if_idle(db, printer_id, None)

def _cancel_job_instance(db: Session, job: Optional[PrintJob], current: User, tasks: Optional[BackgroundTasks] = None) -> PrintJobOut:
    if not job:
        raise HTTPException(404, "Job not found")

    if job.status in {"completed", "failed", "canceled"}:
        raise HTTPException(409, f"status_not_cancelable:{job.status}")

    ok, reason = _can_cancel_with_reason(current, job)
    if not ok:
        raise HTTPException(403, f"Forbidden:{reason}")

    was_processing = (job.status == "processing")

    job.status = "canceled"
    job.finished_at = datetime.utcnow()
    db.add(job); db.commit(); db.refresh(job)

    ev = _format_event(
        type="print.canceled",
        printer_id=job.printer_id,
        data={"name": job.name, "job_id": int(job.id)},
    )
    _submit_bg(tasks, _emit_event_all_channels, db, job.employee_id, ev)

    if was_processing:
        _submit_bg(tasks, _poll_octoprint_ready_and_chain, db, job.printer_id, 30.0)
    else:
        _start_next_job_if_idle(db, job.printer_id, tasks)

    return _to_out(db, current, job)

@router.post("/printers/jobs/{job_id}/cancel", response_model=PrintJobOut)
def cancel_job(
    job_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current: User = Depends(get_confirmed_user),
):
    job = db.query(PrintJob).filter(PrintJob.id == job_id).first()
    return _cancel_job_instance(db, job, current, background_tasks)

@router.post("/printers/{printer_id}/queue/{job_id}/cancel", response_model=PrintJobOut)
def cancel_job_alias_post(
    printer_id: str, job_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current: User = Depends(get_confirmed_user),
):
    pid = _norm_printer_id(printer_id)
    job = db.query(PrintJob).filter(PrintJob.id == job_id, PrintJob.printer_id == pid).first()
    return _cancel_job_instance(db, job, current, background_tasks)

@router.delete("/printers/{printer_id}/queue/{job_id}", response_model=PrintJobOut)
def cancel_job_alias_delete(
    printer_id: str, job_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current: User = Depends(get_confirmed_user),
):
    pid = _norm_printer_id(printer_id)
    job = db.query(PrintJob).filter(PrintJob.id == job_id, PrintJob.printer_id == pid).first()
    return _cancel_job_instance(db, job, current, background_tasks)

# -----------------------------------------------------------------------------#
# pause / resume (per printer)
# -----------------------------------------------------------------------------#
@router.post("/api/printers/{printer_id}/pause")
def pause_current(
    printer_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_confirmed_user),
):
    pid = _norm_printer_id(printer_id)
    job = (db.query(PrintJob)
             .filter(PrintJob.printer_id == pid, PrintJob.status == "processing")
             .order_by(PrintJob.started_at.desc(), PrintJob.id.desc())
             .first())
    if not job:
        raise HTTPException(404, "No processing job")
    if not _owner_or_manager(current, job):
        raise HTTPException(403, "Forbidden")

    job.status = "paused"
    db.add(job); db.commit(); db.refresh(job)
    return {"ok": True, "jobId": job.id, "status": job.status}

@router.post("/api/printers/{printer_id}/resume")
def resume_next(
    printer_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current: User = Depends(get_confirmed_user),
):
    pid = _norm_printer_id(printer_id)
    job = (db.query(PrintJob)
             .filter(PrintJob.printer_id == pid, PrintJob.status == "paused")
             .order_by(PrintJob.uploaded_at.asc(), PrintJob.id.asc())
             .first())
    if not job:
        raise HTTPException(404, "No paused job")
    if not _owner_or_manager(current, job):
        raise HTTPException(403, "Forbidden")

    if RESUME_DIRECT_PROCESSING:
        now = datetime.utcnow()
        job.status = "processing"
        if not job.started_at:
            job.started_at = now
        db.add(job); db.commit(); db.refresh(job)

        _bind_runmap_remote(pid, job)
        return {"ok": True, "jobId": job.id, "status": job.status}
    else:
        job.status = "queued"
        db.add(job); db.commit(); db.refresh(job)
        _start_next_job_if_idle(db, pid, background_tasks)
        return {"ok": True, "jobId": job.id, "status": job.status}

# -----------------------------------------------------------------------------#
# internal: process-next (ต้องมี X-Admin-Token)
# -----------------------------------------------------------------------------#
def _check_admin_token(token: str):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

@router.post("/internal/printers/{printer_id}/queue/process-next")
def internal_process_next(
    printer_id: str,
    background_tasks: BackgroundTasks,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
    x_admin_token: str = Header(default=""),
    x_reason: str = Header(default=""),
    request: Request = None,  # ✅ รับ request จริงเพื่ออ่าน header
):
    _check_admin_token(x_admin_token)
    pid = _norm_printer_id(printer_id)
    if x_reason:
        logger.info("[QUEUE] process-next requested (printer=%s) reason=%s", pid, x_reason)

    reason = "-"
    try:
        if request is not None:
            reason = request.headers.get("X-Reason", "-")
    except Exception:
        pass
    logger.info("[QUEUE] process-next requested (printer=%s) reason=%s force=%s", pid, reason, bool(force))

    has_processing = db.query(PrintJob).filter(
        PrintJob.printer_id == pid,
        PrintJob.status == "processing"
    ).order_by(PrintJob.started_at.desc(), PrintJob.id.desc()).first()

    if has_processing:
        if force:
            has_processing.status = "completed"
            has_processing.finished_at = datetime.utcnow()
            db.add(has_processing); db.commit(); db.refresh(has_processing)
            _notify_job_event_async(has_processing.id, "completed", pid, has_processing.name)

            ev = _format_event(
                type="print.completed",
                printer_id=pid,
                data={"name": has_processing.name, "job_id": int(has_processing.id)},
            )
            _submit_bg(background_tasks, _emit_event_all_channels, db, has_processing.employee_id, ev)
        else:
            pr_state = ""
            pr_progress = 0.0
            try:
                with httpx.Client(timeout=8.0) as c:
                    r = c.get(f"{BACKEND_INTERNAL_BASE}/printers/{pid}/octoprint/job",
                              params={"force": "true"})
                    if r.status_code == 200:
                        mapped = (r.json().get("mapped") or {})
                        pr_state = (mapped.get("state") or "").lower()
                        try:
                            pr_progress = float(mapped.get("progress") or 0.0)
                        except Exception:
                            pr_progress = 0.0
            except Exception:
                pr_state = ""
            if (pr_state and pr_state not in {"printing"}) or pr_progress >= 99.5:
                has_processing.status = "completed"
                has_processing.finished_at = datetime.utcnow()
                db.add(has_processing); db.commit(); db.refresh(has_processing)
                _notify_job_event_async(has_processing.id, "completed", pid, has_processing.name)

                ev = _format_event(
                    type="print.completed",
                    printer_id=pid,
                    data={"name": has_processing.name, "job_id": int(has_processing.id)},
                )
                _submit_bg(background_tasks, _emit_event_all_channels, db, has_processing.employee_id, ev)
            else:
                return {"ok": True, "message": "already-processing", "state": pr_state, "progress": pr_progress}

    job = _start_next_job_if_idle(db, pid, background_tasks)
    if not job:
        return {"ok": True, "message": "no-job-or-bed-not-empty"}
    return {"ok": True, "message": "started", "jobId": job.id}

# -----------------------------------------------------------------------------#
# reorder (only manager)
# -----------------------------------------------------------------------------#
@router.post("/printers/{printer_id}/queue/reorder")
def reorder_queue(
    printer_id: str,
    payload: QueueReorderIn,
    db: Session = Depends(get_db),
    manager: User = Depends(get_manager_user),
):
    pid = _norm_printer_id(printer_id)
    if not payload.job_ids:
        return {"ok": True, "updated": 0}

    rows: List[PrintJob] = (
        db.query(PrintJob)
        .filter(PrintJob.printer_id == pid, PrintJob.id.in_(payload.job_ids))
        .all()
    )
    if not rows:
        return {"ok": True, "updated": 0}

    if any(j.status == "processing" for j in rows):
        raise HTTPException(
            status_code=409,
            detail="reorder_requires_pause_or_cancel_processing",
        )

    movable = {j.id: j for j in rows if j.status in {"queued", "paused"}}
    if not movable:
        return {"ok": True, "updated": 0}

    base = datetime.utcnow()
    updated = 0
    for i, jid in enumerate(payload.job_ids):
        j = movable.get(jid)
        if not j:
            continue
        j.uploaded_at = base + timedelta(seconds=i)
        db.add(j)
        updated += 1

    db.commit()
    return {"ok": True, "updated": updated}
