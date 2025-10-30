# backend/print_queue.py
from __future__ import annotations

import os
import re
import json
import asyncio
import inspect
import mimetypes
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple, Callable, Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Header, Query, Body
from sqlalchemy import case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db import get_db
from auth import (
    get_current_user,
    get_confirmed_user,
    get_manager_user,
    get_optional_user,
)

from models import User, Printer, PrintJob, StorageFile
from schemas import (
    PrintJobCreate, PrintJobPatch, PrintJobOut,
    QueueListOut, QueueReorderIn, CurrentJobOut
)
from notifications import notify_user
from s3util import (
    copy_object, head_object, delete_object, new_storage_key, presign_get,
)
from pydantic import ValidationError

# ถ้ามี put_object ให้ใช้
try:
    from s3util import put_object
except Exception:  # pragma: no cover
    put_object = None

router = APIRouter(tags=["print-queue"])
logger = logging.getLogger("print_queue")

ADMIN_TOKEN = (os.getenv("ADMIN_TOKEN") or "").strip()
DEFAULT_PRINTER_ID = os.getenv("DEFAULT_PRINTER_ID", "prusa-core-one")
BACKEND_INTERNAL_BASE = (os.getenv("BACKEND_INTERNAL_BASE", "http://127.0.0.1:8001")).rstrip("/")

ALLOWED_SOURCE = {"upload", "history", "storage"}

def _check_admin_token(token: str):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

# ---------- OctoPrint settings ----------
def _clean_env(v: Optional[str]) -> str:
    return (v or "").strip().strip('"').strip("'")

OCTO_BASE = _clean_env(os.getenv("OCTOPRINT_BASE") or "").rstrip("/")
OCTO_KEY  = _clean_env(os.getenv("OCTOPRINT_API_KEY") or "")

_timeout_raw = _clean_env(os.getenv("OCTOPRINT_HTTP_TIMEOUT") or os.getenv("OCTOPRINT_TIMEOUT") or "30")
try:
    import re as _re
    _m = _re.match(r"^\d+(\.\d+)?", _timeout_raw)
    OCTO_TIMEOUT = float(_m.group(0)) if _m else 30.0
except Exception:
    OCTO_TIMEOUT = 30.0

PUBLIC_BASE_URL = _clean_env(os.getenv("PUBLIC_BASE_URL") or "")

# ---------------------------------------------------------------------------
# Helpers / Utils
# ---------------------------------------------------------------------------

def status_order_expr():
    return case(
        (PrintJob.status.in_(("processing", "printing")), 0),
        (PrintJob.status == "queued",     1),
        (PrintJob.status == "paused",     2),
        (PrintJob.status == "completed",  3),
        (PrintJob.status == "failed",     4),
        (PrintJob.status == "canceled",   5),
        else_=9,
    ).label("status_rank")

def _emp(x) -> str:
    return str(x or "").strip()

def _norm_printer_id(v: Optional[str]) -> str:
    s = (v or "").strip()
    s = re.sub(r"[^\w\s\-]+", "", s, flags=re.U)
    s = re.sub(r"\s+", "-", s, flags=re.U)
    s = (s or DEFAULT_PRINTER_ID).lower()
    return s

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

def _to_out(db: Session, current_user: User, job: PrintJob, name_map: Optional[Dict[str, str]] = None) -> PrintJobOut:
    j_source = (getattr(job, "source", None) or "").strip().lower()
    if j_source not in ALLOWED_SOURCE:
        j_source = "storage"
    try:
        o = PrintJobOut.model_validate(job, from_attributes=True)
    except ValidationError:
        payload = {k: getattr(job, k, None) for k in [
            "id","printer_id","employee_id","name","thumb","time_min","status",
            "uploaded_at","started_at","finished_at","octoprint_job_id"
        ]}
        payload["source"] = j_source
        o = PrintJobOut.model_validate(payload)
    ok, _ = _can_cancel_with_reason(current_user, job)
    if hasattr(o, "me_can_cancel"):
        o.me_can_cancel = ok
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
    if not ct and lower.endswith(".gcode"):
        ct = "text/plain"
    if not ct and lower.endswith(".stl"):
        ct = "model/stl"
    return ct or default

def _is_gcode_name(name_or_key: str) -> bool:
    lower = (name_or_key or "").lower()
    return lower.endswith((".gcode", ".gco", ".gc"))

# ---------- Background submit helper ----------

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
        pass

# ---------- storage helpers ----------

def _ensure_storage_record(
    db: Session,
    employee_id: str,
    object_key: str,
    filename_hint: Optional[str] = None,
) -> None:
    if not object_key or not object_key.startswith("storage/"):
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
        size = int(h.get("ContentLength", 0) or 0)
        ct = h.get("ContentType") or ct
    except Exception:
        pass
    row = StorageFile(
        employee_id=_emp(employee_id),
        filename=base,
        name=None,
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
            r = c.get(url)
            r.raise_for_status()
            data = r.content
    except Exception as e:
        raise HTTPException(500, f"read_uploads_failed:{src_path}") from e
    try:
        put_object(dst_key, data, _guess_ct(dst_name))
    except Exception as e:
        raise HTTPException(500, f"storage_put_failed:{dst_key}") from e
    return dst_key

def _finalize_object_if_staging(
    db: Session,
    employee_id: str,
    src_key: Optional[str],
    display_name: Optional[str] = None,
    want_record: bool = True,
) -> Optional[str]:
    if not src_key:
        return src_key
    if src_key.startswith("storage/"):
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
        if not _is_gcode_name(src_base):
            logger.info("Skip finalize non-gcode from staging: %s", src_key)
            return src_key
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
    return src_key

# ---------- OctoPrint uploader/dispatcher ----------

def _octo_headers() -> dict:
    return {"X-Api-Key": OCTO_KEY} if OCTO_KEY else {}

def _safe_filename(name: str) -> str:
    n = re.sub(r"[^\w.\-]+", "_", name or "job.gcode")
    if not n.lower().endswith(".gcode"):
        n += ".gcode"
    return n

async def _download_bytes(src: str) -> bytes:
    s = (src or "").strip()
    if not s:
        raise RuntimeError("empty_source")
    async with httpx.AsyncClient(timeout=OCTO_TIMEOUT, follow_redirects=True) as client:
        if s.startswith(("storage/", "catalog/", "staging/", "printer-store/")):
            url = presign_get(s)
            r = await client.get(url)
            r.raise_for_status()
            return r.content
        if s.startswith("/uploads/"):
            if not PUBLIC_BASE_URL:
                raise RuntimeError("PUBLIC_BASE_URL_not_set")
            url = f"{PUBLIC_BASE_URL}{s}"
            r = await client.get(url)
            r.raise_for_status()
            return r.content
        if s.startswith(("http://", "https://")):
            r = await client.get(s)
            r.raise_for_status()
            return r.content
        if os.path.exists(s):
            with open(s, "rb") as f:
                return f.read()
        try:
            url = presign_get(s)
            r = await client.get(url)
            r.raise_for_status()
            return r.content
        except Exception:
            pass
        raise RuntimeError(f"unsupported_source:{src}")

async def _dispatch_to_octoprint(job: PrintJob) -> None:
    if not (OCTO_BASE and OCTO_KEY):
        logger.warning("OctoPrint not configured (base/key missing), skip dispatch")
        return
    src = (getattr(job, "gcode_path", "") or getattr(job, "gcode_key", "") or "").strip()
    if not src:
        logger.error("Job %s has no gcode source (gcode_path/key is empty)", job.id)
        return
    filename = _safe_filename(os.path.basename(src) or f"job_{job.id}.gcode")
    try:
        logger.info("Dispatch job %s to OctoPrint | src=%s", job.id, src)
        file_bytes = await _download_bytes(src)
        files = {"file": (filename, file_bytes, _guess_ct(filename))}
        qs = urlencode({"select": "true", "print": "true"})
        url = f"{OCTO_BASE}/api/files/local?{qs}"
        async with httpx.AsyncClient(timeout=OCTO_TIMEOUT) as client2:
            up = await client2.post(url, headers=_octo_headers(), files=files)
        if up.status_code >= 300:
            logger.error("Octo upload failed %s: %s", up.status_code, up.text)
            raise RuntimeError(f"octoprint_upload_failed:{up.status_code}")
        logger.info("OctoPrint: uploaded & started %s", filename)
    except Exception as e:
        logger.exception("OctoPrint push failed for job %s: %s", job.id, e)
        raise

# ---------- RUNMAP binder (local-first, HTTP fallback) ----------

def _bind_runmap_remote(printer_id: str, job: PrintJob, *, octo_user: str | None = None) -> None:
    """
    ผูก job_id ↔ รอบพิมพ์ปัจจุบัน:
      1) ลองเรียกฟังก์ชันภายในโปรเซส (local) ก่อน
      2) ถ้าไม่สำเร็จค่อย fallback ยิง HTTP (timeout สั้น)
    """
    try:
        from printer_status import _bind_runmap as _bind_runmap_core  # type: ignore
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

# ---------- notifier (local async-first) ----------

def _notify_job_event_async(job_id: int, status_out: str, printer_id: str, name: str | None):
    """
    สร้าง task ที่จะเรียก printer_status._notify_job_event แบบ async ภายในโปรเซส
    ถ้าหาไม่ได้จะ fallback ยิง HTTP ด้วย timeout สั้น
    """
    async def _run():
        try:
            from printer_status import _notify_job_event as _notify_local  # type: ignore
            # local async call
            asyncio.create_task(_notify_local(job_id, status_out, printer_id=printer_id, name=name))
            logger.info("[QUEUE] notify job-event (local) %s #%s", status_out, job_id)
            return
        except Exception:
            logger.exception("[QUEUE] notify local not available, fallback HTTP")
        try:
            base = (os.getenv("BACKEND_INTERNAL_BASE") or "http://127.0.0.1:8001").rstrip("/")
            admin = os.getenv("ADMIN_TOKEN") or ""
            url = f"{base}/notifications/job-event"
            headers = {"X-Admin-Token": admin, "Content-Type": "application/json"}
            payload = {"job_id": job_id, "status": status_out, "printer_id": printer_id, "name": name}
            timeout = httpx.Timeout(5.0, connect=2.0, read=2.0, write=2.0)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
                r = await c.post(url, json=payload, headers=headers)
                logger.info("[QUEUE] notify job-event (HTTP) %s → %s", status_out, r.status_code)
        except Exception:
            logger.exception("[QUEUE] notify job-event (HTTP) failed")
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_run())
    except RuntimeError:
        asyncio.run(_run())

# ---------- start-next helper ----------

def _start_next_job_if_idle(db: Session, printer_id: str, tasks: Optional[BackgroundTasks] = None) -> Optional[PrintJob]:
    printer_id = _norm_printer_id(printer_id)

    has_processing = db.query(PrintJob).filter(
        PrintJob.printer_id == printer_id,
        PrintJob.status == "processing"
    ).first()
    if has_processing:
        return None

    next_job = db.query(PrintJob).filter(
        PrintJob.printer_id == printer_id,
        PrintJob.status == "queued"
    ).order_by(PrintJob.uploaded_at.asc(), PrintJob.id.asc()).first()

    if not next_job:
        return None

    now = datetime.utcnow()
    next_job.status = "processing"
    if not next_job.started_at:
        next_job.started_at = now
    db.add(next_job); db.commit(); db.refresh(next_job)

    _bind_runmap_remote(printer_id, next_job)

    _submit_bg(
        tasks,
        notify_user,
        db, next_job.employee_id,
        type="print.started",
        severity="info",
        title="ถึงคิวพิมพ์ของคุณแล้ว",
        message=next_job.name,
        data={"job_id": next_job.id, "printer_id": next_job.printer_id},
    )

    _submit_bg(tasks, _dispatch_to_octoprint, next_job)

    return next_job

# ---------- คำนวณเวลาคิว/เวลาที่เหลือ ----------

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

# ---------------------------------------------------------------------------
# core enqueue
# ---------------------------------------------------------------------------

def _enqueue_job(db: Session, current: User, payload: PrintJobCreate, printer_id: str, tasks: Optional[BackgroundTasks] = None) -> PrintJobOut:
    printer_id = _norm_printer_id(printer_id)
    _get_or_create_printer(db, printer_id)

    original_key_in = getattr(payload, "original_key", None)
    gcode_key_in  = getattr(payload, "gcode_key", None)
    gcode_path_in = getattr(payload, "gcode_path", None)
    name = payload.name

    gcode_src_in = gcode_path_in or gcode_key_in
    same_key = bool(original_key_in and gcode_src_in and original_key_in == gcode_src_in)

    if original_key_in and not same_key:
        _ = _finalize_object_if_staging(
            db, _emp(current.employee_id),
            original_key_in,
            display_name=name or original_key_in,
            want_record=False,
        )

    gcode_final = None
    if gcode_src_in:
        gcode_final = _finalize_object_if_staging(
            db, _emp(current.employee_id),
            gcode_src_in,
            display_name=name or gcode_src_in,
            want_record=True,
        )

    db.commit()

    job = PrintJob(
        printer_id=printer_id,
        employee_id=_emp(current.employee_id),
        name=name,
        thumb=payload.thumb,
        time_min=payload.time_min,
        source=payload.source,
        gcode_path=gcode_final or gcode_path_in or gcode_key_in,
        status="queued",
        uploaded_at=datetime.utcnow(),
    )

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

    # ไม่ auto-start เมื่อ enqueue — ให้ฝั่ง printer_status ตัดสิน
    return _to_out(db, current, job)

# ---------------------------------------------------------------------------
# duplicate guard helpers
# ---------------------------------------------------------------------------

def _find_recent_duplicate(db: Session, *, employee_id: str, printer_id: str, gcode_path: str, window_sec: int) -> Optional[PrintJob]:
    """
    มองหางาน 'queued' ที่เพิ่งสร้าง (ภายใน window_sec) ของคนเดิม + เครื่องเดิม + gcode เดิม
    """
    if not (employee_id and printer_id and gcode_path):
        return None
    since = datetime.utcnow() - timedelta(seconds=window_sec)
    return (
        db.query(PrintJob)
          .filter(
              PrintJob.employee_id == _emp(employee_id),
              PrintJob.printer_id == _norm_printer_id(printer_id),
              PrintJob.status == "queued",
              PrintJob.uploaded_at >= since,
              PrintJob.gcode_path == gcode_path,
          )
          .order_by(PrintJob.uploaded_at.desc(), PrintJob.id.desc())
          .first()
    )

# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

@router.post("/api/print", response_model=PrintJobOut)
def create_print(
    payload: PrintJobCreate,
    printer_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current: User = Depends(get_confirmed_user),
    background_tasks: BackgroundTasks = None,
    idem_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    pid = _norm_printer_id(printer_id or DEFAULT_PRINTER_ID)

    # ---- duplicate guard (120s ถ้ามี Idempotency-Key, ไม่งั้น 60s) ----
    gpath = (payload.gcode_path or payload.gcode_key or "").strip()
    if gpath:
        win = 3
        dup = _find_recent_duplicate(db,
            employee_id=_emp(current.employee_id),
            printer_id=pid,
            gcode_path=gpath,
            window_sec=win,
        )
        if dup:
            return _to_out(db, current, dup)

    return _enqueue_job(db, current, payload, pid, background_tasks)

@router.post("/printers/{printer_id}/queue", response_model=PrintJobOut)
def enqueue_for_printer(
    printer_id: str,
    payload: PrintJobCreate,
    db: Session = Depends(get_db),
    current: User = Depends(get_confirmed_user),
    background_tasks: BackgroundTasks = None,
    idem_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    pid = _norm_printer_id(printer_id)

    gpath = (payload.gcode_path or payload.gcode_key or "").strip()
    if gpath:
        win = 3
        dup = _find_recent_duplicate(db,
            employee_id=_emp(current.employee_id),
            printer_id=pid,
            gcode_path=gpath,
            window_sec=win,
        )
        if dup:
            return _to_out(db, current, dup)

    return _enqueue_job(db, current, payload, pid, background_tasks)

# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

@router.get("/printers/{printer_id}/queue", response_model=QueueListOut)
def list_queue(
    printer_id: str,
    include_all: bool = True,
    db: Session = Depends(get_db),
    current: Optional[User] = Depends(get_optional_user),
    x_admin_token: str = Header(default=""),
):
    is_admin = bool(ADMIN_TOKEN and x_admin_token and x_admin_token == ADMIN_TOKEN)
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

# ---------------------------------------------------------------------------
# current job
# ---------------------------------------------------------------------------

@router.get("/api/printers/{printer_id}/current-job", response_model=CurrentJobOut)
def current_job_for_printer(
    printer_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    rows: List[PrintJob] = (
        db.query(PrintJob)
          .filter(PrintJob.printer_id == printer_id)
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
                thumbnail_url=cur.thumb or "/images/placeholder-model.png",
                job_id=cur.id,
                status=("processing" if cur.status == "processing" else cur.status),
                started_at=cur.started_at,
                time_min=cur.time_min,
                remaining_min=remaining,
            )
    # Fallback: DB ไม่มี แต่อุปกรณ์กำลังพิมพ์อยู่
    try:
        with httpx.Client(timeout=6.0) as c:
            r = c.get(f"{BACKEND_INTERNAL_BASE}/printers/{printer_id}/octoprint/job", params={"force": "true"})
        if r.status_code == 200:
            m = (r.json().get("mapped") or {})
            if (m.get("state") or "").lower() == "printing":
                sec_left = m.get("time_left") or m.get("timeLeft") or 0
                try:
                    remaining_min = max(int(float(sec_left) // 60), 0)
                except Exception:
                    remaining_min = None
                return CurrentJobOut(
                    queue_number=1,
                    file_name=m.get("file_name") or m.get("file") or "(Printing)",
                    thumbnail_url="/images/placeholder-model.png",
                    job_id=0,  # pseudo
                    status="processing",
                    started_at=None,
                    time_min=None,
                    remaining_min=remaining_min,
                )
    except Exception:
        pass
    raise HTTPException(404, "No active job")

# ---------------------------------------------------------------------------
# patch (rename / change status)
# ---------------------------------------------------------------------------

@router.patch("/printers/jobs/{job_id}", response_model=PrintJobOut)
def patch_job(
    job_id: int,
    payload: PrintJobPatch,
    db: Session = Depends(get_db),
    current: User = Depends(get_confirmed_user),
    background_tasks: BackgroundTasks = None,
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
            _submit_bg(
                background_tasks,
                notify_user,
                db, job.employee_id,
                type="print.started",
                severity="info",
                title="ถึงคิวพิมพ์ของคุณแล้ว",
                message=job.name,
                data={"job_id": job.id, "printer_id": job.printer_id},
            )

        if s in ("completed", "failed", "canceled"):
            job.finished_at = now

        job.status = s

        if s in ("completed", "failed", "canceled"):
            status_out = "cancelled" if s == "canceled" else s
            _notify_job_event_async(job.id, status_out, job.printer_id, job.name)

    db.add(job); db.commit(); db.refresh(job)
    return _to_out(db, current, job)

# ---------------------------------------------------------------------------
# cancel (รวมจุดใช้งาน)
# ---------------------------------------------------------------------------

def _cancel_job_instance(db: Session, job: Optional[PrintJob], current: User, tasks: Optional[BackgroundTasks] = None) -> PrintJobOut:
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status in {"completed", "failed", "canceled"}:
        raise HTTPException(409, f"status_not_cancelable:{job.status}")
    ok, reason = _can_cancel_with_reason(current, job)
    if not ok:
        raise HTTPException(403, f"Forbidden:{reason}")
    job.status = "canceled"
    job.finished_at = datetime.utcnow()
    db.add(job); db.commit(); db.refresh(job)
    _submit_bg(
        tasks,
        notify_user,
        db, job.employee_id,
        type="print.canceled",
        title="ยกเลิกงานพิมพ์",
        message=job.name,
        severity="info",
        data={"job_id": job.id},
    )
    return _to_out(db, current, job)

@router.post("/printers/jobs/{job_id}/cancel", response_model=PrintJobOut)
def cancel_job(
    job_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_confirmed_user),
    background_tasks: BackgroundTasks = None,
):
    job = db.query(PrintJob).filter(PrintJob.id == job_id).first()
    return _cancel_job_instance(db, job, current, background_tasks)

@router.post("/printers/{printer_id}/queue/{job_id}/cancel", response_model=PrintJobOut)
def cancel_job_alias_post(
    printer_id: str,
    job_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_confirmed_user),
    background_tasks: BackgroundTasks = None,
):
    pid = _norm_printer_id(printer_id)
    job = (
        db.query(PrintJob)
          .filter(PrintJob.id == job_id, PrintJob.printer_id == pid)
          .first()
    )
    return _cancel_job_instance(db, job, current, background_tasks)

@router.delete("/printers/{printer_id}/queue/{job_id}", response_model=PrintJobOut)
def cancel_job_alias_delete(
    printer_id: str,
    job_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_confirmed_user),
    background_tasks: BackgroundTasks = None,
):
    pid = _norm_printer_id(printer_id)
    job = (
        db.query(PrintJob)
          .filter(PrintJob.id == job_id, PrintJob.printer_id == pid)
          .first()
    )
    return _cancel_job_instance(db, job, current, background_tasks)

# ---------------------------------------------------------------------------
# pause / resume (ระดับเครื่อง)
# ---------------------------------------------------------------------------

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
    db: Session = Depends(get_db),
    current: User = Depends(get_confirmed_user),
    background_tasks: BackgroundTasks = None,
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

    now = datetime.utcnow()
    job.status = "processing"              # <<<< เดิมเป็น "queued"
    if not job.started_at:
        job.started_at = now
    db.add(job); db.commit(); db.refresh(job)

    # ผูก runmap ให้ฝั่ง printer_status หา active ได้
    _bind_runmap_remote(pid, job)

    # (ถ้าต้องการ) แจ้งเจ้าของงานว่าเริ่มต่อแล้ว
    _submit_bg(
        background_tasks,
        notify_user,
        db, job.employee_id,
        type="print.started",
        severity="info",
        title="กลับมาพิมพ์ต่อแล้ว",
        message=job.name,
        data={"job_id": job.id, "printer_id": job.printer_id},
    )

    return {"ok": True, "jobId": job.id, "status": job.status}

# ---------------------------------------------------------------------------
# internal: process-next
# ---------------------------------------------------------------------------

@router.post("/internal/printers/{printer_id}/queue/process-next")
def internal_process_next(
    printer_id: str,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None,
    x_admin_token: str = Header(default="")
):
    _check_admin_token(x_admin_token)
    pid = _norm_printer_id(printer_id)

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
                pr_progress = 0.0

            if (pr_state and pr_state not in {"printing"}) or pr_progress >= 99.5:
                has_processing.status = "completed"
                has_processing.finished_at = datetime.utcnow()
                db.add(has_processing); db.commit(); db.refresh(has_processing)
                _notify_job_event_async(has_processing.id, "completed", pid, has_processing.name)
            else:
                return {"ok": True, "message": "already-processing", "state": pr_state, "progress": pr_progress}

    job = _start_next_job_if_idle(db, pid, background_tasks)
    if not job:
        return {"ok": True, "message": "no-job"}
    return {"ok": True, "message": "started", "jobId": job.id}

# ---------------------------------------------------------------------------
# reorder (only manager)
# ---------------------------------------------------------------------------

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
    base = datetime.utcnow()
    updated = 0
    for i, jid in enumerate(payload.job_ids):
        job = db.query(PrintJob).filter(PrintJob.id == jid, PrintJob.printer_id == pid).first()
        if not job:
            continue
        job.uploaded_at = base + timedelta(seconds=i)
        db.add(job); updated += 1
    db.commit()
    return {"ok": True, "updated": updated}
