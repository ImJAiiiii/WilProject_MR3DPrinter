# backend/printer_status.py
from __future__ import annotations

import asyncio, json, os, re, logging
from datetime import datetime, timedelta
from typing import Dict, Set, Optional, Tuple, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Body, Query, Header, Cookie
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse, Response
from sqlalchemy.orm import Session
from sqlalchemy import or_, func
import httpx
import urllib.parse
import unicodedata

from db import get_db, SessionLocal
from models import Printer, User, PrintJob
from schemas import (
    PrinterStatusOut,
    PrinterHeartbeatIn,
    PrinterStatusUpdateIn,
)
from auth import get_confirmed_user, decode_token

router = APIRouter(prefix="/printers", tags=["printers"])
log = logging.getLogger("printer_status")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    log.addHandler(_h)
log.setLevel(logging.INFO)
log.propagate = True  # ให้เด้งไป root ด้วย

# ==============================
# Config / Defaults
# ==============================
def _clean_env(v: Optional[str]) -> str:
    return (v or "").strip().strip('"').strip("'")

ONLINE_TTL        = int(_clean_env(os.getenv("PRINTER_ONLINE_TTL")) or "30")
SNAPSHOT_URL      = _clean_env(os.getenv("SNAPSHOT_URL"))
OCTO_BASE         = (_clean_env(os.getenv("OCTOPRINT_BASE")) or "").rstrip("/")
OCTO_KEY          = _clean_env(os.getenv("OCTOPRINT_API_KEY"))
ADMIN_TOKEN       = _clean_env(os.getenv("ADMIN_TOKEN"))
BACKEND_INTERNAL_BASE = (_clean_env(os.getenv("BACKEND_INTERNAL_BASE")) or "http://127.0.0.1:8001").rstrip("/")
AUTO_NOTIFY_ON_READY_PROGRESS = _clean_env(os.getenv("AUTO_NOTIFY_ON_READY_PROGRESS") or "1").lower() not in {"0", "false"}

_timeout_raw = _clean_env(os.getenv("OCTOPRINT_HTTP_TIMEOUT") or os.getenv("OCTOPRINT_TIMEOUT") or "8.0")
try:
    OCTO_TIMEOUT = float(re.match(r"^\d+(\.\d+)?", _timeout_raw).group(0))
except Exception:
    OCTO_TIMEOUT = 8.0

OCTO_MIN_INTERVAL = float(_clean_env(os.getenv("OCTOPRINT_MIN_INTERVAL")) or "2.0")
OCTO_502_COOLDOWN = float(_clean_env(os.getenv("OCTOPRINT_502_COOLDOWN")) or "60.0")

AUTO_HEAL_ATTACH = _clean_env(os.getenv("AUTO_HEAL_ATTACH") or "1").lower() not in {"0", "false"}

# --- complete guard: กันยิงซ้ำหลังเพิ่งแจ้งสำเร็จ ---
_COMPLETE_GUARD_UNTIL: Dict[str, float] = {}  # printer_id -> unix_ts
COMPLETE_GUARD_TTL = float(os.getenv("COMPLETE_GUARD_TTL", "30"))

# --- cancel/fail guard: กัน SAFEGAURD ปิดเป็น completed หลังเพิ่ง cancel/failed ---
_CANCEL_GUARD_UNTIL: Dict[str, float] = {}  # printer_id -> unix_ts
CANCEL_GUARD_TTL = float(os.getenv("CANCEL_GUARD_TTL", "120"))

def _octo_ready() -> bool:
    return bool(OCTO_BASE and OCTO_KEY)

def _octo_headers() -> Dict[str, str]:
    return {"X-Api-Key": OCTO_KEY, "Accept": "application/json"}

# in-memory rate-limit / cache / cooldown ต่อเครื่อง
_OCTO_LAST_CALL: Dict[str, float] = {}
_OCTO_LAST_DATA: Dict[str, dict] = {}
_OCTO_COOLDOWN_UNTIL: Dict[str, float] = {}

# ==============================
# Normalize helper
# ==============================
_SLUG_RE = re.compile(r"[^\w\-]+", flags=re.U)
def _norm_pid(v: Optional[str]) -> str:
    s = (v or "").strip().lower()
    s = _SLUG_RE.sub("-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "default"

# ==============================
# Auth helper: Bearer | Cookie | X-Admin-Token
# ==============================
async def admin_or_confirmed(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_admin: str | None = Header(default=None, alias="X-Admin-Token"),
    cookie_token: str | None = Cookie(default=None, alias="access_token"),
    cookie_bearer: str | None = Cookie(default=None, alias="Authorization"),
):
    if x_admin and ADMIN_TOKEN and x_admin == ADMIN_TOKEN:
        return {"role": "admin"}

    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()

    if not token:
        token = (cookie_token or "").strip()
    if not token and cookie_bearer:
        token = cookie_bearer.replace("Bearer ", "").strip()

    if not token:
        raise HTTPException(401, "Not authenticated")

    try:
        payload = decode_token(token)
        sub = str(payload.get("sub") or "")
        if not sub:
            raise HTTPException(401, "Invalid token")
        return {"role": "user", "sub": sub}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Invalid token")

# ==============================
# Pub/Sub (SSE)
# ==============================
class StatusBus:
    def __init__(self) -> None:
        self._subs: Dict[str, Set[asyncio.Queue]] = {}

    async def publish(self, printer_id: str, payload: dict):
        for q in list(self._subs.get(printer_id, set())):
            try:
                await q.put(payload)
            except Exception:
                self._subs.get(printer_id, set()).discard(q)

    def subscribe(self, printer_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(printer_id, set()).add(q)
        return q

    def unsubscribe(self, printer_id: str, q: asyncio.Queue):
        self._subs.get(printer_id, set()).discard(q)
        if not self._subs.get(printer_id):
            self._subs.pop(printer_id, None)

bus = StatusBus()

# ============ RUN-MAP (bind job ↔ current run per printer) ============
_RUNMAP: dict[str, dict] = {}  # key = printer_id → {"job_id":int,"employee_id":str,"name":str,"octo_user":str,"ts":iso}

def _bind_runmap(printer_id: str, *, job_id: int, employee_id: str, name: str, octo_user: str|None=None) -> None:
    pid = _norm_pid(printer_id)
    _RUNMAP[pid] = {
        "job_id": int(job_id),
        "employee_id": (employee_id or "").strip(),
        "name": (name or "").strip(),
        "octo_user": (octo_user or "").strip() if octo_user else "",
        "ts": datetime.utcnow().isoformat(),
    }
    log.info("[RUNMAP] bind %s → job#%s owner=%s name='%s'", pid, job_id, employee_id, name)

def _peek_runmap(printer_id: str) -> dict|None:
    return _RUNMAP.get(_norm_pid(printer_id))

def _clear_runmap(printer_id: str) -> None:
    _RUNMAP.pop(_norm_pid(printer_id), None)
    log.info("[RUNMAP] clear %s", _norm_pid(printer_id))

def _reconcile_active_with_runmap(db: Session, printer_id: str) -> Optional[PrintJob]:
    """ถ้ามี active job และมี runmap → ปรับ owner/name ให้ตรง runmap"""
    pid = _norm_pid(printer_id)
    rm = _peek_runmap(pid)
    if not rm:
        return None
    active = _find_active_job(db, pid)
    if not active:
        return None
    changed = False
    if rm.get("employee_id") and active.employee_id != rm["employee_id"]:
        active.employee_id = rm["employee_id"]; changed = True
    if rm.get("name") and active.name != rm["name"]:
        active.name = rm["name"]; changed = True
    if changed:
        active.updated_at = datetime.utcnow()
        db.add(active); db.commit(); db.refresh(active)
        log.info("[RUNMAP] reconcile active #%s → owner=%s name='%s'", active.id, active.employee_id, active.name)
        return active
    return None

# ==============================
# Helpers (DB/Model)
# ==============================
def _is_online(p: Printer) -> bool:
    if not p.last_heartbeat_at:
        return False
    return (datetime.utcnow() - p.last_heartbeat_at) <= timedelta(seconds=ONLINE_TTL)

def _to_out(p: Printer) -> PrinterStatusOut:
    state = p.state or "ready"
    is_on = _is_online(p)
    if not is_on:
        state = "offline"
    return PrinterStatusOut(
        printer_id=p.id,
        display_name=p.display_name,
        is_online=is_on,
        state=state,
        status_text=p.status_text or ("Printer is ready" if state == "ready" else state.title()),
        progress=p.progress,
        temp_nozzle=p.temp_nozzle,
        temp_bed=p.temp_bed,
        updated_at=p.updated_at or datetime.utcnow(),
    )

def _get_or_create_printer(db: Session, printer_id: str) -> Printer:
    pid = _norm_pid(printer_id)
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

def _sse_format(data: str, event: str = "message") -> str:
    return f"event: {event}\ndata: {data}\n\n"

def _find_active_job(db: Session, printer_id: str) -> Optional[PrintJob]:
    pid = _norm_pid(printer_id)
    return (
        db.query(PrintJob)
        .filter(
            PrintJob.printer_id == pid,
            or_(PrintJob.status == "processing", PrintJob.status == "printing")
        )
        .order_by(PrintJob.started_at.desc(), PrintJob.id.desc())
        .first()
    )

def _oldest_queued_job(db: Session, printer_id: str) -> Optional[PrintJob]:
    pid = _norm_pid(printer_id)
    return (
        db.query(PrintJob)
        .filter(PrintJob.printer_id == pid, PrintJob.status == "queued")
        .order_by(PrintJob.uploaded_at.asc(), PrintJob.id.asc())
        .first()
    )

def _create_pseudo_job(db: Session, printer_id: str, file_name: Optional[str] = None) -> PrintJob:
    """สร้างงาน processing เฉพาะเมื่อไม่มีงานใน DB จริงๆ และพยายามยืม owner จากคิวที่ชื่อไฟล์ใกล้สุด"""
    pid = _norm_pid(printer_id)
    borrowed_owner = None
    near = _find_queued_job_by_filename(db, pid, file_name or "")
    if near and near.employee_id:
        borrowed_owner = near.employee_id

    j = PrintJob(
        printer_id=pid,
        employee_id=borrowed_owner or "octoprint",
        name=(file_name or "(Printing)"),
        source="octoprint",
        gcode_path=None,
        status="processing",
        uploaded_at=datetime.utcnow(),
        started_at=datetime.utcnow(),
    )
    db.add(j); db.commit(); db.refresh(j)
    log.info("[PSEUDO] create job #%s for %s (%s) owner=%s", j.id, pid, j.name, j.employee_id)
    return j

# ===== helper: normalize & match file name =====
def _norm_file(s: Optional[str]) -> str:
    s = (s or "").strip()
    s = s.replace("\\", "/")
    s = s.rsplit("/", 1)[-1]
    s = urllib.parse.unquote(s)
    s = unicodedata.normalize("NFKC", s)
    s = s.lower().strip()
    s = re.sub(r"\.(gcode|gco|gc|g|ufp|zip)$", "", s)
    s = re.sub(r"\((copy|[0-9]+)\)$", "", s)
    s = re.sub(r"(_copy|-copy|_export|-export)$", "", s)
    s = s.replace("_", " ").replace("-", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _find_queued_job_by_filename(db: Session, printer_id: str, cur_fname: str) -> Optional[PrintJob]:
    if not cur_fname:
        return None
    pid = _norm_pid(printer_id)
    target = _norm_file(cur_fname)

    cand: list[PrintJob] = (
        db.query(PrintJob)
          .filter(PrintJob.printer_id == pid, PrintJob.status == "queued")
          .order_by(PrintJob.uploaded_at.desc(), PrintJob.id.desc())
          .limit(100).all()
    )

    for j in cand:
        if _norm_file(j.name) == target:
            return j
    for j in cand:
        jj = _norm_file(j.name)
        if jj.startswith(target) or target.startswith(jj):
            return j
    t2 = target.replace(" ", "")
    for j in cand:
        jj2 = _norm_file(j.name).replace(" ", "")
        if jj2.startswith(t2) or t2.startswith(jj2):
            return j
    return None

def _complete_queued_job_by_filename(db: Session, printer_id: str, file_name: str) -> Optional[PrintJob]:
    """ถ้าเจองานในคิวที่ชื่อไฟล์ตรง/ใกล้เคียง ให้ปิดเป็น completed (ใช้แทนการสร้าง pseudo)"""
    pid = _norm_pid(printer_id)
    q = _find_queued_job_by_filename(db, pid, file_name or "")
    if not q:
        return None
    now = datetime.utcnow()
    # ยกเลิกสถานะ queued แล้วปิดเป็น completed ตรง ๆ
    q.status = "completed"
    if not q.started_at:
        q.started_at = now
    q.finished_at = now
    q.progress = 100.0
    db.add(q); db.commit(); db.refresh(q)
    log.info("[SAFEGUARD] complete queued-by-filename #%s ('%s')", q.id, q.name)
    return q

def _adopt_owner_and_name_from(db: Session, active: PrintJob, queued: PrintJob) -> None:
    changed = False
    if queued.employee_id and active.employee_id != queued.employee_id:
        active.employee_id = queued.employee_id; changed = True
    if queued.name and active.name != queued.name:
        active.name = queued.name; changed = True
    if not active.uploaded_at and queued.uploaded_at:
        active.uploaded_at = queued.uploaded_at; changed = True
    if queued.status == "queued":
        queued.status = "canceled"
        queued.finished_at = datetime.utcnow()
        db.add(queued)
    if changed:
        active.updated_at = datetime.utcnow()
        db.add(active)
    db.commit(); db.refresh(active)

def _reconcile_active_with_queue(db: Session, printer_id: str, cur_fname: str) -> Optional[PrintJob]:
    pid = _norm_pid(printer_id)
    active = _find_active_job(db, pid)
    if not active or (active.employee_id or "").strip().lower() != "octoprint":
        return None
    matched = _find_queued_job_by_filename(db, pid, cur_fname)
    if not matched:
        return None
    _adopt_owner_and_name_from(db, active, matched)
    log.info("[RECONCILE] adopt queued #%s → active #%s (owner=%s, name='%s')",
             matched.id, active.id, active.employee_id, active.name)
    return active

def _promote_latest_paused_to_processing(db: Session, printer_id: str) -> Optional[PrintJob]:
    """ถ้ามีงานค้าง paused ให้ปลุกกลับมาเป็น processing ก่อนใช้ AUTO_HEAL"""
    pid = _norm_pid(printer_id)
    j = (
        db.query(PrintJob)
          .filter(PrintJob.printer_id == pid, PrintJob.status == "paused")
          .order_by(PrintJob.started_at.desc().nullslast(), PrintJob.id.desc())
          .first()
    )
    if not j:
        return None
    if not j.started_at:
        j.started_at = datetime.utcnow()
    j.status = "processing"
    db.add(j); db.commit(); db.refresh(j)
    log.info("[HEAL] promote paused→processing #%s '%s'", j.id, j.name)
    return j

# ==============================
# REST: Basic status / heartbeat
# ==============================
@router.get("/{printer_id}/status", response_model=PrinterStatusOut)
def get_status(printer_id: str, db: Session = Depends(get_db)):
    p = _get_or_create_printer(db, printer_id)
    return _to_out(p)

@router.post("/{printer_id}/heartbeat", response_model=PrinterStatusOut)
async def heartbeat(printer_id: str, data: PrinterHeartbeatIn, db: Session = Depends(get_db)):
    p = _get_or_create_printer(db, printer_id)
    p.last_heartbeat_at = datetime.utcnow()
    if data.progress is not None:
        p.progress = max(0.0, min(100.0, float(data.progress)))
    if data.temp_nozzle is not None:
        p.temp_nozzle = float(data.temp_nozzle)
    if data.temp_bed is not None:
        p.temp_bed = float(data.temp_bed)
    if data.status_text:
        p.status_text = data.status_text
    p.updated_at = datetime.utcnow()
    db.add(p); db.commit(); db.refresh(p)

    payload = {"type": "status", "data": _to_out(p)}
    await bus.publish(p.id, payload)
    return _to_out(p)

@router.put("/{printer_id}/status", response_model=PrinterStatusOut)
async def update_status(printer_id: str, data: PrinterStatusUpdateIn, db: Session = Depends(get_db)):
    p = _get_or_create_printer(db, printer_id)
    changed = False
    if data.state:
        p.state = data.state; changed = True
    if data.status_text is not None:
        p.status_text = data.status_text; changed = True
    if data.progress is not None:
        p.progress = max(0.0, min(100.0, float(data.progress))); changed = True
    if data.temp_nozzle is not None:
        p.temp_nozzle = float(data.temp_nozzle); changed = True
    if data.temp_bed is not None:
        p.temp_bed = float(data.temp_bed); changed = True

    if changed:
        p.updated_at = datetime.utcnow()
        db.add(p); db.commit(); db.refresh(p)
        await bus.publish(p.id, {"type": "status", "data": _to_out(p)})

    return _to_out(p)

# ==============================
# SSE stream
# ==============================
@router.get("/{printer_id}/status/stream")
async def stream_status(printer_id: str, request: Request):
    db = SessionLocal()
    try:
        p = _get_or_create_printer(db, printer_id)
        init_payload = {"type": "status", "data": _to_out(p)}
    finally:
        db.close()

    init_json = json.dumps(jsonable_encoder(init_payload))
    init = _sse_format(init_json, event="status")

    queue = bus.subscribe(printer_id)

    async def gen():
        yield init
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=20)
                    enc = json.dumps(jsonable_encoder(msg))
                    yield _sse_format(enc, event=msg.get("type", "status"))
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            bus.unsubscribe(printer_id, queue)

    return StreamingResponse(gen(), media_type="text/event-stream")

# ==============================
# Snapshot proxy (กล้อง)
# ==============================
@router.get("/{printer_id}/snapshot")
async def proxy_snapshot(printer_id: str, request: Request, src: Optional[str] = None):
    url = (src or SNAPSHOT_URL or "").strip()
    if not url:
        raise HTTPException(503, "SNAPSHOT_URL is not configured")

    ts = int(datetime.utcnow().timestamp() * 1000)
    url = f"{url}{'&' if '?' in url else '?'}ts={ts}"

    try:
        async with httpx.AsyncClient(timeout=OCTO_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(url, headers={"Accept": "image/*"})
            r.raise_for_status()
            headers = {
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Content-Type": r.headers.get("Content-Type", "image/jpeg"),
            }
            return Response(content=r.content, headers=headers, media_type=headers["Content-Type"])
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, f"Snapshot HTTP {e.response.status_code}")
    except Exception as e:
        raise HTTPException(502, f"Snapshot fetch failed: {e}")

# ==============================
# OctoPrint integration
# ==============================
def _map_octo_state(state_text: str) -> Tuple[str, str]:
    s = (state_text or "").lower()
    if "printing" in s:
        return "printing", "Printing..."
    if "paus" in s:
        return "paused", "Paused"
    if "error" in s or "fail" in s:
        return "error", "Error"
    if "offline" in s or "closed" in s:
        return "offline", "Offline"
    if "operational" in s:
        return "ready", "Printer is ready"
    return "ready", "Printer is ready"

async def _fetch_octo_job_and_printer() -> Tuple[dict, dict]:
    async with httpx.AsyncClient(timeout=OCTO_TIMEOUT) as client:
        job_r = await client.get(f"{OCTO_BASE}/api/job", headers=_octo_headers())
        prn_r = await client.get(f"{OCTO_BASE}/api/printer", headers=_octo_headers())
        job_r.raise_for_status(); prn_r.raise_for_status()
        return job_r.json(), prn_r.json()

def _read_octo_temps_payload_sync() -> dict:
    if not _octo_ready():
        raise HTTPException(503, "OctoPrint is not configured")
    import requests
    try:
        r = requests.get(f"{OCTO_BASE}/api/printer", headers=_octo_headers(), timeout=OCTO_TIMEOUT)
        r.raise_for_status()
        prn = r.json()
        t = prn.get("temperature") or {}
        tool0 = t.get("tool0") or {}
        bed   = t.get("bed")   or {}
        return {
            "ok": True,
            "temperature": t,
            "nozzle": {"actual": tool0.get("actual"), "target": tool0.get("target")},
            "bed":    {"actual": bed.get("actual"),   "target": bed.get("target")},
        }
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else 502
        raise HTTPException(status, f"OctoPrint HTTP {status}")
    except Exception as e:
        raise HTTPException(502, f"OctoPrint request failed: {e}")

# ---------- internal auto-chain caller ----------
async def _call_process_next(printer_id: str, force: bool = True) -> dict:
    pid = _norm_pid(printer_id)
    if not ADMIN_TOKEN:
        return {"ok": False, "error": "missing_admin_token"}
    url = f"{BACKEND_INTERNAL_BASE}/internal/printers/{pid}/queue/process-next"
    if force:
        url += "?force=1"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, headers={"X-Admin-Token": ADMIN_TOKEN})
            log.info("[AUTO-CHAIN] POST %s -> %s %s", url, r.status_code, r.text[:200])
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"http_{e.response.status_code}"}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__}

# ---------- internal notifier to /notifications/job-event ----------
async def _notify_job_event(job_id: int, status: str, *, printer_id: str,
                            name: str | None = None,
                            detected_class: str | None = None,
                            confidence: float | None = None):
    if not ADMIN_TOKEN:
        logging.warning("[OCTO] skip notify_job_event: missing ADMIN_TOKEN")
        return False
    url = f"{BACKEND_INTERNAL_BASE}/notifications/job-event"
    headers = {"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json"}
    payload = {
        "job_id": job_id,
        "status": status,
        "printer_id": printer_id,
        "name": name,
        "detected_class": detected_class,
        "confidence": confidence,
    }
    # รัด timeout ให้สั้นและสมดุล
    timeout = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
            r = await c.post(url, json=payload, headers=headers)
            logging.info("[OCTO] notify %s → %s %s", status, r.status_code, r.text[:200])
            r.raise_for_status()
            return True
    except httpx.ReadTimeout:
        logging.warning("[OCTO] notify %s → ReadTimeout (will ignore)", status)
        return False
    except httpx.HTTPError as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        logging.warning("[OCTO] notify %s → HTTPError %s", status, code)
        return False
    except Exception:
        logging.exception("[OCTO] notify %s → unexpected error", status)
        return False

# ---------- RUNMAP internal endpoint (bind from queue) ----------
@router.post("/{printer_id}/internal/runmap/bind")
async def bind_runmap(
    printer_id: str,
    body: dict = Body(..., example={"job_id": 123, "employee_id": "emp001", "name": "Part_A"}),
    x_admin: str | None = Header(default=None, alias="X-Admin-Token"),
):
    if not (ADMIN_TOKEN and x_admin == ADMIN_TOKEN):
        raise HTTPException(401, "Unauthorized")
    try:
        _bind_runmap(
            printer_id,
            job_id=int(body.get("job_id")),
            employee_id=str(body.get("employee_id") or ""),
            name=str(body.get("name") or ""),
            octo_user=str(body.get("octo_user") or ""),
        )
        return {"ok": True}
    except Exception as e:
        raise HTTPException(422, f"invalid payload: {e}")

@router.get("/{printer_id}/octoprint/job")
async def octoprint_job(
    printer_id: str,
    force: bool = Query(default=False, description="ข้ามแคช/คูลดาวน์ (ใช้ระวัง)"),
):
    if not _octo_ready():
        raise HTTPException(503, "OctoPrint is not configured")

    pid = _norm_pid(printer_id)

    # --- rate limit / cache ---
    now_ts = datetime.utcnow().timestamp()
    last = _OCTO_LAST_CALL.get(pid, 0.0)
    cooldown_until = _OCTO_COOLDOWN_UNTIL.get(pid, 0.0)

    if not force and now_ts < cooldown_until:
        cached = _OCTO_LAST_DATA.get(pid)
        if cached:
            return cached

    if not force and (now_ts - last) < OCTO_MIN_INTERVAL:
        cached = _OCTO_LAST_DATA.get(pid)
        if cached:
            return cached

    # --- fetch from OctoPrint ---
    try:
        job, prn = await _fetch_octo_job_and_printer()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 502:
            _OCTO_COOLDOWN_UNTIL[pid] = now_ts + OCTO_502_COOLDOWN
        raise HTTPException(e.response.status_code, f"OctoPrint HTTP {e.response.status_code}")
    except Exception as e:
        raise HTTPException(502, f"OctoPrint request failed: {e}")

    _OCTO_LAST_CALL[pid] = now_ts

    # --- map state/progress ---
    try:
        progress = float(job.get("progress", {}).get("completion") or 0.0)
    except Exception:
        progress = 0.0
    state_text = (job.get("state") or "")
    mapped_state, mapped_text = _map_octo_state(state_text)
    log.info("[OCTO] raw state=%s progress=%s", state_text, progress)

    nozzle = prn.get("temperature", {}).get("tool0", {}).get("actual")
    bed    = prn.get("temperature", {}).get("bed", {}).get("actual")

    # --- DB updates & safeguards ---
    db = SessionLocal()
    try:
        p = _get_or_create_printer(db, pid)
        p.state = mapped_state
        p.status_text = mapped_text
        p.progress = max(0.0, min(100.0, progress))
        if nozzle is not None:
            p.temp_nozzle = float(nozzle)
        if bed is not None:
            p.temp_bed = float(bed)
        p.last_heartbeat_at = datetime.utcnow()
        p.updated_at = datetime.utcnow()
        db.add(p); db.commit(); db.refresh(p)

        await bus.publish(p.id, {"type": "status", "data": _to_out(p)})

        counts = dict(
            db.query(PrintJob.status, func.count())
              .filter(PrintJob.printer_id == pid)
              .group_by(PrintJob.status)
              .all()
        )
        log.info("[AUTO-CHAIN] queue-stats pid=%s %s", pid, counts)

        log.info("[AUTO-CHAIN] check: state=%s progress=%.2f", p.state, p.progress or 0.0)
        active = _find_active_job(db, pid)

        # ✅ ใช้ runmap ก่อนเสมอ
        _ = _reconcile_active_with_runmap(db, pid)
        active = _find_active_job(db, pid)

        if p.state == "printing" and AUTO_HEAL_ATTACH:
            # 0) ถ้ายังไม่มี active → ลองปลุก paused ก่อน
            if not active:
                revived = _promote_latest_paused_to_processing(db, pid)
                if revived:
                    active = revived

            if not active:
                # 1) ไม่มี paused/active → พยายามจับคู่ queued ด้วยชื่อไฟล์
                cur_file = ((job.get("job") or {}).get("file") or {})
                cur_name = cur_file.get("display") or cur_file.get("name") or ""

                matched = _find_queued_job_by_filename(db, pid, cur_name)
                if matched:
                    now = datetime.utcnow()
                    matched.status = "processing"
                    if not matched.started_at:
                        matched.started_at = now
                    db.add(matched); db.commit(); db.refresh(matched)
                    log.info("[AUTO-HEAL] attach matched queued #%s ('%s')", matched.id, matched.name)
                else:
                    # 2) สุดท้ายจริงๆ ค่อยสร้าง pseudo เพื่อสะท้อนว่าเครื่องกำลังพิมพ์
                    _create_pseudo_job(db, pid, cur_name or "(Printing)")
                    log.info("[AUTO-HEAL] create pseudo job (octo printing '%s' not found in queue)", cur_name)
            else:
                # มี active แล้ว → พยายาม sync owner/name จาก queued ให้ถูกต้อง
                cur_file = ((job.get("job") or {}).get("file") or {})
                cur_name = cur_file.get("display") or cur_file.get("name") or ""
                reconciled = _reconcile_active_with_queue(db, pid, cur_name)
                if reconciled:
                    log.info("[AUTO-HEAL] reconciled active job owner/name from queue")
                else:
                    log.info("[AUTO-HEAL] active exists → keep as-is")

        else:
            recent = (
                db.query(PrintJob)
                  .filter(PrintJob.printer_id == pid)
                  .order_by(PrintJob.id.desc())
                  .limit(3).all()
            )
            if recent:
                snap = ", ".join(f"#{r.id}:{getattr(r, 'status', '')}" for r in recent)
                log.info("[AUTO-CHAIN] debug recent jobs: %s", snap)
            log.info("[AUTO-CHAIN] skip: no processing job")

            # ----- SAFEGUARD: ไม่มี webhook แต่จบจริง → complete & notify -----
            try:
                nowts = datetime.utcnow().timestamp()

                # ถ้าเพิ่ง cancel/failed → ห้ามปิดเป็น completed ภายในหน้าต่างเวลา
                cg = _CANCEL_GUARD_UNTIL.get(pid, 0.0)
                if nowts < cg:
                    log.info("[SAFEGUARD] skip (cancel/fail guard active %.0fs)", cg - nowts)
                else:
                    # อ่าน snapshot ก่อนหน้า เพื่อจับทรานซิชัน printing→ready หรือ progress ก่อนหน้าแตะ 99.9
                    prev = _OCTO_LAST_DATA.get(pid) or {}
                    prev_mapped = (prev.get("mapped") or {})
                    prev_state = (prev_mapped.get("state") or "").strip().lower()
                    try:
                        prev_prog = float(((prev.get("octoprint") or {}).get("progress") or {}).get("completion") or 0.0)
                    except Exception:
                        prev_prog = 0.0

                    cur_prog = float(p.progress or 0.0)
                    cur_state = (p.state or "").strip().lower()

                    log.info(
                        "[SAFEGUARD] check prev_state=%s prev_prog=%.2f cur_state=%s cur_prog=%.2f",
                        prev_state, prev_prog, cur_state, cur_prog
                    )

                    should_close = (cur_prog >= 99.9) or (
                        cur_state == "ready" and (prev_state in ("printing", "paused") or prev_prog >= 99.9)
                    )

                    if should_close:
                        closed: Optional[PrintJob] = None

                        active_now = _find_active_job(db, pid)
                        if active_now:
                            closed = _complete_current_job_in_db(db, pid, status="completed")
                        else:
                            cur_file = ((job.get("job") or {}).get("file") or {})
                            cur_name = cur_file.get("display") or cur_file.get("name") or ""
                            closed = _complete_queued_job_by_filename(db, pid, cur_name)
                            if not closed:
                                closed = _complete_latest_processing_job(db, pid, status="completed")

                        if not closed:
                            log.info("[SAFEGUARD] nothing to close (skip notify) pid=%s", pid)
                        else:
                            log.info("[SAFEGUARD] complete & notify job #%s", closed.id)
                            ok = await _notify_job_event(closed.id, "completed", printer_id=pid, name=closed.name)
                            if ok:
                                _COMPLETE_GUARD_UNTIL[pid] = datetime.utcnow().timestamp() + COMPLETE_GUARD_TTL
                            else:
                                log.warning("[SAFEGUARD] notify completed returned False")
            except Exception:
                log.exception("[SAFEGUARD] block error")

    finally:
        db.close()

    # auto-chain ต่อถ้าจบ
    if mapped_state == "ready" and (progress or 0.0) >= 99.9:
        log.info("[AUTO-CHAIN] decide: state=ready progress=%.2f", progress or 0.0)
        if os.getenv("AUTO_CHAIN_ON_READY_PROGRESS", "0").lower() not in {"0", "false"}:
            log.info("[AUTO-CHAIN] trigger → process-next (force) [by READY_PROGRESS env]")
            _ = await _call_process_next(pid, force=True)
        else:
            log.info("[AUTO-CHAIN] skipped: READY_PROGRESS auto-chain disabled (wait for bed-empty)")

    payload = {
        "ok": True,
        "octoprint": job,
        "temps": prn.get("temperature"),
        "mapped": _to_out(p).model_dump(mode="json"),
    }
    _OCTO_LAST_DATA[pid] = payload
    return payload


@router.post("/{printer_id}/octoprint/command")
async def octoprint_command(
    printer_id: str,
    body: dict = Body(..., example={"command": "pause", "action": "pause"}),
    _user = Depends(admin_or_confirmed),
):
    if not _octo_ready():
        raise HTTPException(503, "OctoPrint is not configured")

    cmd = (body.get("command") or "").lower().strip()
    url = f"{OCTO_BASE}/api/job"

    if cmd not in {"pause", "cancel"}:
        raise HTTPException(422, "command must be 'pause' or 'cancel'")

    payload = {"command": cmd}
    if cmd == "pause":
        action = (body.get("action") or "pause").lower().strip()
        if action not in {"pause", "resume", "toggle"}:
            raise HTTPException(422, "pause action must be 'pause' or 'resume' or 'toggle'")
        payload["action"] = action

    try:
        async with httpx.AsyncClient(timeout=OCTO_TIMEOUT) as client:
            r = await client.post(
                url,
                headers={**_octo_headers(), "Content-Type": "application/json"},
                json=payload
            )
            r.raise_for_status()
            return {"ok": True}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 502:
            _OCTO_COOLDOWN_UNTIL[_norm_pid(printer_id)] = datetime.utcnow().timestamp() + OCTO_502_COOLDOWN
        raise HTTPException(e.response.status_code, f"OctoPrint HTTP {e.response.status_code}")
    except Exception as e:
        raise HTTPException(502, f"OctoPrint request failed: {e}")

# --- Wrappers (Pause/Cancel)
@router.post("/cmd/pause")
async def cmd_pause(
    body: dict = Body(..., example={"action": "toggle", "printer_id": "prusa-core-one"}),
    _u = Depends(admin_or_confirmed),
):
    printer_id = (body.get("printer_id") or os.getenv("DEFAULT_PRINTER_ID") or "").strip()
    if not printer_id:
        raise HTTPException(422, "printer_id is required (or set DEFAULT_PRINTER_ID env)")
    action = (body.get("action") or "toggle").lower().strip()
    # reuse main handler (now supports 'toggle')
    return await octoprint_command(printer_id, {"command": "pause", "action": action}, _u)

@router.post("/{printer_id}/cancel")
async def cancel_job(printer_id: str, _u = Depends(admin_or_confirmed)):
    pid = _norm_pid(printer_id)

    # 1) ยิงคำสั่งยกเลิกไป OctoPrint (ถ้าพัง เราก็ยังปิด DB ฝั่งเรา)
    try:
        res = await octoprint_command(printer_id, {"command": "cancel"})
    except HTTPException as e:
        log.warning("[CMD] cancel → OctoPrint error HTTP %s (will still close DB job)", e.status_code)
        res = {"ok": False, "error": f"octoprint_http_{e.status_code}"}
    except Exception as e:
        log.exception("[CMD] cancel → OctoPrint request failed (will still close DB job)")
        res = {"ok": False, "error": "octoprint_request_failed"}

    # 2) ตั้ง guard กัน SAFEGAURD ปิดเป็น completed ทับหลังเพิ่ง cancel
    _CANCEL_GUARD_UNTIL[pid] = datetime.utcnow().timestamp() + CANCEL_GUARD_TTL

    # 3) ปิดงานที่กำลังทำใน DB เป็น "canceled" และแจ้งเตือนทันที
    db = SessionLocal()
    try:
        j = _complete_current_job_in_db(db, pid, status="canceled")
        if j:
            log.info("[CMD] cancel → closed job #%s (%s) as canceled", j.id, j.name)
            try:
                ok = await _notify_job_event(j.id, "cancelled", printer_id=pid, name=j.name)
                if not ok:
                    log.warning("[CMD] cancel → notify cancelled returned False")
            except Exception:
                log.exception("[CMD] cancel → notify cancelled failed")
        else:
            log.info("[CMD] cancel → no active job to close for %s", pid)
    finally:
        db.close()

    return {"ok": True, "octoprint": res}

def cancel_queued_job(db: Session, job_id: int) -> bool:
    j = db.query(PrintJob).filter(PrintJob.id == job_id, PrintJob.status == "queued").first()
    if not j:
        return False
    j.status = "canceled"          # 👈 DB ใช้ canceled
    j.finished_at = datetime.utcnow()
    db.add(j); db.commit()
    return True

# ==============================
# Temperature & Speed controls
# ==============================
@router.get("/{printer_id}/octoprint/temps")
async def octoprint_temps(printer_id: str, _u = Depends(admin_or_confirmed)):
    return _read_octo_temps_payload_sync()

@router.get("/public/{printer_id}/octoprint/temps")
def octoprint_temps_public(printer_id: str):
    return _read_octo_temps_payload_sync()

@router.post("/{printer_id}/octoprint/temperature")
async def octoprint_set_temperature(printer_id: str, body: dict = Body(...), _u = Depends(admin_or_confirmed)):
    if not _octo_ready():
        raise HTTPException(503, "OctoPrint is not configured")

    nozzle = body.get("nozzle", None)
    bed    = body.get("bed", None)
    if nozzle is None and bed is None:
        raise HTTPException(400, "need 'nozzle' or 'bed'")

    timeout = httpx.Timeout(OCTO_TIMEOUT, connect=OCTO_TIMEOUT, read=OCTO_TIMEOUT, write=OCTO_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        results: Dict[str, str] = {}

        if nozzle is not None:
            nz = float(nozzle)
            if nz < 0 or nz > 300:
                raise HTTPException(422, "nozzle target must be 0–300°C")
            payload = {"command": "target", "targets": {"tool0": nz}}
            url = f"{OCTO_BASE}/api/printer/tool"
            r = await client.post(url, headers={**_octo_headers(), "Content-Type": "application/json"}, json=payload)
            log.info("[TEMP] tool0→%s | %s %s", nz, r.status_code, r.text[:200])
            r.raise_for_status()
            results["nozzle"] = "ok"

        if bed is not None:
            bd = float(bed)
            if bd < 0 or bd > 130:
                raise HTTPException(422, "bed target must be 0–130°C")
            payload = {"command": "target", "target": bd}
            url = f"{OCTO_BASE}/api/printer/bed"
            r = await client.post(url, headers={**_octo_headers(), "Content-Type": "application/json"}, json=payload)
            log.info("[TEMP] bed→%s | %s %s", bd, r.status_code, r.text[:200])
            r.raise_for_status()
            results["bed"] = "ok"

    return {"ok": True, "applied": results}

@router.post("/{printer_id}/octoprint/feedrate")
async def octoprint_set_feedrate(
    printer_id: str,
    body: dict = Body(..., example={"factor": 100}),
    _u = Depends(admin_or_confirmed),
):
    if not _octo_ready():
        raise HTTPException(503, "OctoPrint is not configured")
    try:
        factor = int(body.get("factor"))
    except Exception:
        raise HTTPException(422, "factor must be integer (10–200)")
    if factor < 10 or factor > 200:
        raise HTTPException(422, "factor must be 10–200 (%)")

    try:
        async with httpx.AsyncClient(timeout=OCTO_TIMEOUT) as client:
            payload = {"command": "feedrate", "factor": factor}
            r = await client.post(f"{OCTO_BASE}/api/printer/printhead", headers={**_octo_headers(), "Content-Type": "application/json"}, json=payload)
            r.raise_for_status()
            return {"ok": True}
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, f"OctoPrint HTTP {e.response.status_code}")
    except Exception as e:
        raise HTTPException(502, f"OctoPrint request failed: {e}")

@router.post("/{printer_id}/temp/tool")
async def set_tool_temp(
    printer_id: str,
    body: dict,
    _u = Depends(admin_or_confirmed),
):
    if not _octo_ready():
        raise HTTPException(503, "OctoPrint is not configured")
    try:
        target = float(body.get("target", 0))
    except Exception:
        raise HTTPException(422, "target must be a number")
    if target < 0 or target > 300:
        raise HTTPException(422, "target must be 0–300°C")
    payload = {"command": "target", "targets": {"tool0": target}}
    async with httpx.AsyncClient(timeout=OCTO_TIMEOUT) as client:
        r = await client.post(f"{OCTO_BASE}/api/printer/tool", headers={**_octo_headers(), "Content-Type": "application/json"}, json=payload)
        r.raise_for_status()
        return {"ok": True}

@router.post("/{printer_id}/temp/bed")
async def set_bed_temp(
    printer_id: str,
    body: dict,
    _u = Depends(admin_or_confirmed),
):
    if not _octo_ready():
        raise HTTPException(503, "OctoPrint is not configured")
    try:
        target = float(body.get("target", 0))
    except Exception:
        raise HTTPException(422, "target must be a number")
    if target < 0 or target > 130:
        raise HTTPException(422, "target must be 0–130°C")
    payload = {"command": "target", "target": target}
    async with httpx.AsyncClient(timeout=OCTO_TIMEOUT) as client:
        r = await client.post(f"{OCTO_BASE}/api/printer/bed", headers={**_octo_headers(), "Content-Type": "application/json"}, json=payload)
        r.raise_for_status()
        return {"ok": True}

@router.post("/{printer_id}/speed")
async def set_feedrate(
    printer_id: str,
    body: dict,
    _u = Depends(admin_or_confirmed),
):
    if not _octo_ready():
        raise HTTPException(503, "OctoPrint is not configured")
    try:
        factor = int(body.get("factor", 100))
    except Exception:
        raise HTTPException(422, "factor must be an integer (10–200)")
    if factor < 10 or factor > 200:
        raise HTTPException(422, "factor must be 10–200 (%)")
    payload = {"command": "feedrate", "factor": factor}
    async with httpx.AsyncClient(timeout=OCTO_TIMEOUT) as client:
        r = await client.post(f"{OCTO_BASE}/api/printer/printhead", headers={**_octo_headers(), "Content-Type": "application/json"}, json=payload)
        r.raise_for_status()
        return {"ok": True}

# ==============================
# Internal helpers for webhook close
# ==============================
def _complete_current_job_in_db(db: Session, printer_id: str, status: str = "completed") -> Optional[PrintJob]:
    pid = _norm_pid(printer_id)
    job = (
        db.query(PrintJob)
          .filter(
              PrintJob.printer_id == pid,
              or_(PrintJob.status == "processing", PrintJob.status == "printing")
          )
          .order_by(PrintJob.started_at.desc(), PrintJob.id.desc())
          .first()
    )
    if not job:
        log.info("[WEBHOOK] no active job to close for %s", pid)
        return None

    job.status = status
    job.finished_at = datetime.utcnow()
    if status == "completed":
        job.progress = 100.0
    db.add(job)

    prn = db.query(Printer).filter(Printer.id == pid).first()
    if prn and getattr(prn, "current_job_id", None):
        prn.current_job_id = None
        prn.updated_at = datetime.utcnow()
        db.add(prn)

    db.commit(); db.refresh(job)
    log.info("[WEBHOOK] job #%s -> %s", job.id, status)
    return job

def _complete_latest_processing_job(db: Session, printer_id: str, status: str = "completed") -> Optional[PrintJob]:
    """fallback: ถ้า _find_active_job ไม่ได้ ให้หา processing/printing ล่าสุดแล้วปิด"""
    pid = _norm_pid(printer_id)
    j = (
        db.query(PrintJob)
          .filter(
              PrintJob.printer_id == pid,
              PrintJob.status.in_(("processing","printing"))
          )
          .order_by(PrintJob.started_at.desc().nullslast(), PrintJob.id.desc())
          .first()
    )
    if not j:
        log.info("[SAFEGUARD] no latest processing/printing to close for %s", pid)
        return None
    j.status = status
    j.finished_at = datetime.utcnow()
    if status == "completed":
        j.progress = 100.0
    db.add(j); db.commit(); db.refresh(j)
    log.info("[SAFEGUARD] closed latest job #%s -> %s", j.id, status)
    return j

# ==============================
# Webhook จาก OctoPrint
# ==============================
@router.post("/{printer_id}/octoprint/webhook")
async def octoprint_webhook(printer_id: str, request: Request, db: Session = Depends(get_db)):
    pid = _norm_pid(printer_id)

    # --- อ่าน raw + content-type ---
    try:
        ctype = (request.headers.get("content-type") or "").lower()
        raw = await request.body()
    except Exception:
        ctype, raw = "", b""

    # --- แปลงข้อมูล: รองรับทั้ง JSON และ x-www-form-urlencoded ---
    payload = {}
    try:
        if "application/json" in ctype:
            payload = await request.json()
        else:
            form = await request.form()
            # OctoPrint มักส่ง field ชื่อ "event" และ "payload" (เป็น JSON string บ้าง/แผนราบบ้าง)
            payload = dict(form)
            # ถ้า payload เป็นสตริง JSON ให้ลองแตก
            pp = payload.get("payload")
            if isinstance(pp, str):
                try:
                    import json as _json
                    payload["payload"] = _json.loads(pp)
                except Exception:
                    pass
    except Exception:
        pass

    # --- ดึง event + data ตามรูปแบบ OctoPrint ---
    event = (payload.get("event") or payload.get("type") or "").strip().lower()
    data  = payload.get("payload") or {}

    log.info("[WEBHOOK] recv pid=%s ctype=%s event=%s keys=%s raw=%s",
             pid, ctype, event, list(data.keys()) if isinstance(data, dict) else type(data).__name__,
             (raw or b"")[:300])

    # map state text ของเรา
    mapping = {
        "printstarted": ("printing", "Printing..."),
        "printdone":    ("ready",    "Printer is ready"),
        "printfailed":  ("error",    "Print failed"),
        "printpaused":  ("paused",   "Paused"),
        "printresumed": ("printing", "Printing..."),
        "startup":      ("ready",    "Printer is ready"),
        "shutdown":     ("offline",  "Offline"),
        "printcanceled": ("ready", "Printer is ready"),
        "printcancelled": ("ready", "Printer is ready"),
    }
    state, text = mapping.get(event, (None, None))

    # อัปเดตสถานะเครื่อง + broadcast
    upd = PrinterStatusUpdateIn(
        state=state,
        status_text=text,
        progress=(data.get("progress") if isinstance(data, dict) else None),
    )
    p_out = await update_status(pid, upd, db)

    # ==== จัดการงานในคิว + notify ====
    # ดึงชื่อไฟล์แท้ (OctoPrint อาจส่ง name/filename)
    def _file_from(d):
        if isinstance(d, dict):
            return d.get("name") or d.get("filename") or ""
        return ""

    file_name = _file_from(data)

    if event == "printstarted":
        active = _find_active_job(db, pid)
        if not active:
            matched = _find_queued_job_by_filename(db, pid, file_name)
            if matched:
                now = datetime.utcnow()
                matched.status = "processing"
                if not matched.started_at:
                    matched.started_at = now
                db.add(matched); db.commit(); db.refresh(matched)
                log.info("[WEBHOOK] PrintStarted → attach queued #%s ('%s')", matched.id, matched.name)
            else:
                _create_pseudo_job(db, pid, file_name or "(Printing)")
                log.info("[WEBHOOK] PrintStarted → create pseudo ('%s')", file_name)
        else:
            _ = _reconcile_active_with_queue(db, pid, file_name)

    elif event == "printdone":
        job = _complete_current_job_in_db(db, pid, status="completed")
        if job:
            log.info("[WEBHOOK] PrintDone → job #%s completed", job.id)
            try:
                await _notify_job_event(job.id, "completed", printer_id=pid, name=job.name)
            except Exception:
                log.exception("[WEBHOOK] notify completed failed")

    elif event == "printfailed":
        _CANCEL_GUARD_UNTIL[pid] = datetime.utcnow().timestamp() + CANCEL_GUARD_TTL
        job = _complete_current_job_in_db(db, pid, status="failed")
        if job:
            log.info("[WEBHOOK] PrintFailed → job #%s failed", job.id)
            try:
                await _notify_job_event(job.id, "failed", printer_id=pid, name=job.name)
            except Exception:
                log.exception("[WEBHOOK] notify failed failed")

    elif event in {"printcanceled", "printcancelled"}:
        _CANCEL_GUARD_UNTIL[pid] = datetime.utcnow().timestamp() + CANCEL_GUARD_TTL
        job = _complete_current_job_in_db(db, pid, status="canceled")
        if job:
            log.info("[WEBHOOK] PrintCanceled → job #%s canceled", job.id)
            try:
                await _notify_job_event(job.id, "cancelled", printer_id=pid, name=job.name)
            except Exception:
                log.exception("[WEBHOOK] notify cancelled failed")

    elif event == "printresumed":
        # ดึงงานที่ pause ล่าสุด แล้วปลุกกลับมาเป็น processing
        job = (
            db.query(PrintJob)
            .filter(PrintJob.printer_id == pid, PrintJob.status == "paused")
            .order_by(PrintJob.started_at.desc().nullslast(), PrintJob.id.desc())
            .first()
        )
        if job:
            job.status = "processing"
            if not job.started_at:
                job.started_at = datetime.utcnow()
            db.add(job); db.commit(); db.refresh(job)
            log.info("[WEBHOOK] PrintResumed → job #%s resumed (%s)", job.id, job.name)
        # (ไม่ต้องสร้างงานใหม่ ไม่ต้องจับคิว)

    return p_out

# --- DEBUG: quick snapshot of queue vs octoprint ---
@router.get("/internal/debug/{printer_id}/queue-snapshot")
def debug_queue_snapshot(printer_id: str):
    pid = _norm_pid(printer_id)
    db = SessionLocal()
    try:
        counts = dict(
            db.query(PrintJob.status, func.count())
              .filter(PrintJob.printer_id == pid)
              .group_by(PrintJob.status)
              .all()
        )
        active = (
            db.query(PrintJob)
              .filter(
                  PrintJob.printer_id == pid,
                  or_(PrintJob.status == "processing", PrintJob.status == "printing", PrintJob.status == "paused")
              )
              .order_by(PrintJob.started_at.desc().nullslast(), PrintJob.id.desc())
              .first()
        )
        last10 = (
            db.query(PrintJob)
              .filter(PrintJob.printer_id == pid)
              .order_by(PrintJob.id.desc())
              .limit(10).all()
        )
        last10_dump = [
            {
                "id": j.id, "status": j.status, "name": j.name,
                "uploaded_at": j.uploaded_at, "started_at": j.started_at, "finished_at": j.finished_at,
                "employee_id": j.employee_id
            } for j in last10
        ]
    finally:
        db.close()

    octo = None
    if _octo_ready():
        try:
            job, prn = httpx.Client(timeout=OCTO_TIMEOUT).get(f"{OCTO_BASE}/api/job", headers=_octo_headers()), None
        except Exception:
            job, prn = None, None
        try:
            prn = httpx.Client(timeout=OCTO_TIMEOUT).get(f"{OCTO_BASE}/api/printer", headers=_octo_headers())
        except Exception:
            pass
        try:
            job = job.json() if job is not None else None
            prn = prn.json() if prn is not None else None
        except Exception:
            pass
        octo = {
            "state": (job or {}).get("state"),
            "progress": (job or {}).get("progress", {}).get("completion"),
            "file": (((job or {}).get("job") or {}).get("file") or {}),
        }

    return {
        "printer_id": pid,
        "db_counts": counts,
        "db_active": ({"id": active.id, "status": active.status, "name": active.name} if active else None),
        "db_last10": last10_dump,
        "octoprint": octo,
    }

# ---------- UNIVERSAL OctoPrint event endpoint (debug-friendly) ----------
@router.post("/octoprint/events")
async def octoprint_events(
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        ctype = (request.headers.get("content-type") or "").lower()
        raw = await request.body()
        logging.info("[OCTO] hit %s len=%s ctype=%s", request.url.path, len(raw or b""), ctype)
    except Exception:
        raw = b""

    data = {}
    try:
        if "application/json" in ctype:
            data = await request.json()
        else:
            form = await request.form()
            data = dict(form)
    except Exception:
        pass

    hdr_evt = request.headers.get("X-Event") or request.headers.get("X-Octo-Event")
    event = (data.get("event") or data.get("type") or hdr_evt or "").strip().lower()
    payload = data.get("payload") or data

    printer_id = (
        (payload.get("printer_id") if isinstance(payload, dict) else None)
        or data.get("printer_id")
        or os.getenv("DEFAULT_PRINTER_ID", "")
    ).strip().lower() or "-"

    job_id = None
    name = None

    def _log_job(j):
        logging.info("[OCTO] resolved job → id=%s name=%s status=%s", getattr(j,"id",None), getattr(j,"name",None), getattr(j,"status",None))

    try:
        if isinstance(payload, dict):
            job_id = payload.get("job_id") or payload.get("id")
            name = payload.get("name")

        if not job_id:
            j = (
                db.query(PrintJob)
                  .filter(
                      PrintJob.printer_id == printer_id,
                      PrintJob.status.in_(("processing","printing","paused"))
                  )
                  .order_by(PrintJob.started_at.desc(), PrintJob.id.desc())
                  .first()
            )
            if j:
                job_id, name = j.id, j.name
                _log_job(j)

        if not job_id:
            since = datetime.utcnow() - timedelta(hours=12)
            j = (
                db.query(PrintJob)
                  .filter(
                      PrintJob.printer_id == printer_id,
                      PrintJob.started_at >= since
                  )
                  .order_by(PrintJob.id.desc())
                  .first()
            )
            if j:
                job_id, name = j.id, j.name
                _log_job(j)
    except Exception:
        logging.exception("[OCTO] job resolving error")

    logging.info("[OCTO] event=%s printer=%s job_id=%s name=%s body=%s",
                 event, printer_id, job_id, name, (raw or b"")[:400])

    status_map = {
        "printdone": "completed",
        "print_done": "completed",
        "done": "completed",
        "completed": "completed",
        "printfailed": "failed",
        "print_failed": "failed",
        "failed": "failed",
        "error": "failed",
        "printcanceled": "cancelled",
        "printcancelled": "cancelled",
        "print_canceled": "cancelled",
        "print_cancelled": "cancelled",
        "cancel": "cancelled",
        "cancelled": "cancelled",
        "canceled": "cancelled",
    }
    mapped = status_map.get(event)

    notify_result = None
    if mapped and job_id:
        try:
            notify_result = await _notify_job_event(
                job_id, mapped, printer_id=printer_id, name=name
            )
            logging.info("[OCTO] notify %s → ok", mapped)
        except httpx.HTTPError as e:
            sc = getattr(e.response, "status_code", 0)
            logging.exception("[OCTO] notify %s failed http %s", mapped, sc)
            notify_result = {"ok": False, "http_status": sc}
        except Exception:
            logging.exception("[OCTO] notify %s failed", mapped)
            notify_result = {"ok": False, "error": "notify_exception"}

    return {
        "ok": True,
        "event": event,
        "mapped": mapped or None,
        "printer_id": printer_id,
        "job_id": job_id,
        "name": name,
        "notified": bool(notify_result),
        "notify_result": notify_result,
    }
