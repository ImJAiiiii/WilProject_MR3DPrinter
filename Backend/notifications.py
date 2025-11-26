# backend/notifications.py
from __future__ import annotations

import os, re, asyncio, json, logging, inspect, time, threading
from datetime import datetime, timedelta, timezone
from typing import Dict, Set, Optional, Iterable, List, Tuple, Literal, Union, Any
from collections import deque

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Query, Header, WebSocket, WebSocketDisconnect, status
from fastapi.responses import StreamingResponse, HTMLResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel

from db import get_db, SessionLocal
from models import Notification, NotificationTarget, User, PrintJob
from schemas import NotificationOut, NotificationCreate, NotificationMarkRead
from auth import get_user_from_header_or_query  # covers Header / ?token=
from emailer import send_notification_email
from teams_flow_webhook import notify_dm
from models import LatencyLog

# =============================================================================
# Logger
# =============================================================================
log = logging.getLogger("notifications")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    log.addHandler(_h)
log.setLevel(logging.INFO)
log.propagate = True

# =============================================================================
# Fire & forget helper (async/sync-safe)
# =============================================================================
def _spawn(func_or_coro, /, *args, **kwargs) -> None:
    """Run background work regardless of sync/async context."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if inspect.iscoroutine(func_or_coro):
        if loop:
            loop.create_task(func_or_coro)
        else:
            def _runner_co():
                try:
                    asyncio.run(func_or_coro)
                except Exception:
                    log.exception("[BG] worker (coroutine) error")
            threading.Thread(target=_runner_co, daemon=True).start()
        return

    if inspect.iscoroutinefunction(func_or_coro):
        if loop:
            loop.create_task(func_or_coro(*args, **kwargs))
        else:
            def _runner_af():
                try:
                    asyncio.run(func_or_coro(*args, **kwargs))
                except Exception:
                    log.exception("[BG] worker (async fn) error")
            threading.Thread(target=_runner_af, daemon=True).start()
        return

    if loop:
        loop.run_in_executor(None, lambda: func_or_coro(*args, **kwargs))
        return

    def _runner_fn():
        try:
            func_or_coro(*args, **kwargs)
        except Exception:
            log.exception("[BG] worker (sync fn) error")

    threading.Thread(target=_runner_fn, daemon=True).start()

# =============================================================================
# ENV / CONFIG
# =============================================================================
def _clean_env(v: Optional[str]) -> str:
    return (v or "").strip().strip('"').strip("'")

ADMIN_TOKEN = _clean_env(os.getenv("ADMIN_TOKEN"))
DEFAULT_PRINTER_ID = _clean_env(os.getenv("DEFAULT_PRINTER_ID"))
BACKEND_INTERNAL_BASE = (_clean_env(os.getenv("BACKEND_INTERNAL_BASE")) or "http://127.0.0.1:8001").rstrip("/")

# FE/BE base URLs for deep-links in DMs
FRONTEND_BASE_URL = _clean_env(os.getenv("FRONTEND_BASE_URL")).rstrip("/")
PUBLIC_BASE_URL   = (_clean_env(os.getenv("PUBLIC_BASE_URL")) or FRONTEND_BASE_URL).rstrip("/")
DM_TITLE = (_clean_env(os.getenv("DM_TITLE")) or "ADI 3D Printer Console")

# ===== Feature toggles =====
def _as_bool(s: Optional[str], default: bool=False) -> bool:
    if s is None: return default
    return s.strip().lower() not in {"0","false","no","off"}

def _as_list_csv(s: Optional[str]) -> list[str]:
    return [tok.strip().lower() for tok in (s or "").split(",") if tok.strip()]

EMAIL_ENABLED = _as_bool(os.getenv("EMAIL_ENABLED"), False)
HOLOLENS_MIRROR_USER_NOTIFS = _as_bool(os.getenv("HOLOLENS_MIRROR_USER_NOTIFS"), True)
HOLOLENS_MIRROR_EVENT_NAME  = (_clean_env(os.getenv("HOLOLENS_MIRROR_EVENT_NAME")) or "toast")

# ==== DM policy ===============================================================
DM_ENABLED = _as_bool(os.getenv("DM_ENABLED"), True)

# parse DM types ให้รับทั้งแบบมี/ไม่มี "print." prefix
def _parse_types_csv(s: str | None) -> set[str]:
    out: set[str] = set()
    for tok in (s or "").split(","):
        k = tok.strip().lower()
        if not k:
            continue
        if k.startswith("print."):
            out.add(k)
            st = k.split(".", 1)[1]
            out.add(st)
        else:
            out.add(k)
            out.add("print."+k)
    return out

_DM_DEFAULT = "print.queued,print.started,print.processing,print.completed,print.failed,print.canceled,print.paused,print.issue"
DM_FOR_TYPES_SET: Set[str] = _parse_types_csv(os.getenv("DM_FOR_TYPES") or _DM_DEFAULT)

DM_REQUIRE_JOB_ID = _as_bool(os.getenv("DM_REQUIRE_JOB_ID"), True)
DM_REQUIRE_PRINTER_ID = _as_bool(os.getenv("DM_REQUIRE_PRINTER_ID"), True)

# Timezone helper (Bangkok time)
_TZ_BKK = timezone(timedelta(hours=7))
def _fmt_bkk(dt: Optional[datetime] = None) -> str:
    d = (dt or datetime.utcnow()).astimezone(_TZ_BKK)
    return d.strftime("%d %b %Y %H:%M")

async def _call_internal(path: str, *, reason: str | None = None) -> dict:
    url = f"{BACKEND_INTERNAL_BASE}{path}"
    headers = {"X-Admin-Token": ADMIN_TOKEN}
    if reason:
        headers["X-Reason"] = reason
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, headers=headers)
            r.raise_for_status()
            return r.json()
    except Exception:
        log.exception("[internal-call] POST %s failed", url)
        return {"ok": False, "error": "internal_call_failed"}

# Detector policy
ALLOWED_DETECT_CLASSES = {
    s.strip().lower() for s in (_clean_env(os.getenv("ALLOWED_DETECT_CLASSES")) or "cracks,layer_shift,spaghetti,stringing").split(",")
    if s.strip()
}
def _as_float(s: Optional[str], default: float) -> float:
    try:
        return float(_clean_env(s) or default)
    except Exception:
        return float(default)

MIN_DETECT_CONFIDENCE = _as_float(os.getenv("MIN_DETECT_CONFIDENCE"), 0.70)
ALERT_ON_UPDATE = _as_bool(os.getenv("ALERT_ON_UPDATE"), True)
BED_EMPTY_BROADCAST = _as_bool(os.getenv("BED_EMPTY_BROADCAST"), False)

# Auto-pause policy
AUTO_PAUSE = _as_bool(os.getenv("AUTO_PAUSE_ON_DETECT"), True)
PAUSE_ON_EVENTS  = {s.strip().lower() for s in (_clean_env(os.getenv("PAUSE_ON_EVENTS")) or "issue_started,issue_update").split(",") if s.strip()}
PAUSE_ON_CLASSES = {s.strip().lower() for s in (_clean_env(os.getenv("PAUSE_ON_CLASSES")) or "cracks,layer_shift,spaghetti,stringing").split(",") if s.strip()}
PAUSE_MIN_CONF   = _as_float(os.getenv("PAUSE_MIN_CONFIDENCE"), 0.70)

# Debounce confirm
DETECT_CONFIRM_ENABLED       = _as_bool(os.getenv("DETECT_CONFIRM_ENABLED"), True)
DETECT_CONFIRM_WINDOW_SEC    = _as_float(os.getenv("DETECT_CONFIRM_WINDOW_SEC"), 5.0)
DETECT_CONFIRM_MIN_HITS      = int(_as_float(os.getenv("DETECT_CONFIRM_MIN_HITS"), 2))
DETECT_CONFIRM_MIN_MEAN_CONF = _as_float(os.getenv("DETECT_CONFIRM_MIN_MEAN_CONF"), 0.78)

# OctoPrint
OCTO_BASE = _clean_env(os.getenv("OCTOPRINT_BASE")).rstrip("/")
OCTO_KEY  = _clean_env(os.getenv("OCTOPRINT_API_KEY"))
try:
    OCTO_TIMEOUT = float(_clean_env(os.getenv("OCTOPRINT_HTTP_TIMEOUT")) or _clean_env(os.getenv("OCTOPRINT_TIMEOUT")) or "10")
except Exception:
    OCTO_TIMEOUT = 10.0

def _octo_ready() -> bool: return bool(OCTO_BASE and OCTO_KEY)
def _octo_headers() -> dict: return {"X-Api-Key": OCTO_KEY, "Accept": "application/json"}

router = APIRouter(prefix="/notifications", tags=["notifications"])

# =============================================================================
# Canonical helpers (status/type/severity)
# =============================================================================
_CANONICAL_TYPES = {
    "print.queued", "print.started", "print.processing",
    "print.completed", "print.failed", "print.canceled",
    "print.paused", "print.issue", "print.pickup_required",
}
_CANON_STATUS = {
    "queued","started","processing","completed","failed","canceled","paused","issue","pickup_required"
}

def _canon_status(s: str | None) -> str:
    ss = (s or "").strip().lower()
    if ss in {"queue"}: ss = "queued"
    if ss in {"cancelled","cancel"}: ss = "canceled"
    if ss.endswith("ing") and ss == "starting": ss = "started"
    return ss

def _norm_event_type(t: Optional[str], status: Optional[str]=None) -> str:
    s = (status or "").strip().lower()
    tt = (t or "").strip().lower()
    if tt in _CANONICAL_TYPES:
        return tt
    if tt == "print.cancelled":
        return "print.canceled"
    if tt == "job-event" and s:
        if s == "cancelled": s = "canceled"
        if s in _CANON_STATUS:
            return f"print.{s}"
    if tt == "print_issue":
        return "print.issue"
    return "print.issue"

def _canon_type(t: str | None, status: str | None) -> str:
    tt = (t or "").strip().lower()
    ss = _canon_status(status)
    if tt in _CANONICAL_TYPES:
        return tt
    if tt in _CANON_STATUS:
        return f"print.{tt}"
    if tt == "cancelled":
        return "print.canceled"
    if tt == "queue":
        return "print.queued"
    if tt.startswith("print.") and tt.replace("print.cancelled","print.canceled") in _CANONICAL_TYPES:
        return tt.replace("print.cancelled","print.canceled")
    if ss:
        return f"print.{ss}"
    return _norm_event_type(t, status)

def _norm_severity_from_type(t: str, sev_in: Optional[str]) -> str:
    """
    ฝั่งสคีมา (NotificationOut) ยอมรับเฉพาะ: info | success | warning | error
    - map critical -> error
    - map neutral  -> warning
    - default mappingตามชนิดอีเวนต์
    """
    s = (sev_in or "").strip().lower()
    if s in {"success","info","warning","error"}:
        return s
    if s == "critical":
        return "error"
    if s == "neutral":
        return "warning"
    return {
        "print.completed": "success",
        "print.failed":    "error",
        "print.paused":    "warning",
        "print.canceled":  "warning",
        "print.issue":     "error",
    }.get(t, "info")

# === NEW: normalize severity when reading old rows from DB ====================
_ALLOWED_SEVERITIES = {"info","success","warning","error"}
def _normalize_severity_for_out(s: Optional[str]) -> str:
    ss = (s or "").strip().lower()
    if ss in _ALLOWED_SEVERITIES:
        return ss
    if ss == "critical":
        return "error"
    # รวมทุกค่าแปลก ๆ เช่น "neutral", "", None → "info" (หรือจะเลือก "warning" ก็ได้)
    return "info"

# =============================================================================
# Bed watcher (internal only; no public "bed empty" user notification)
# =============================================================================
WATCH_BED_EMPTY_TIMEOUT_SEC = int(_as_float(os.getenv("WATCH_BED_EMPTY_TIMEOUT_SEC"), 300))  # 5 min

_BED_EMPTY_TS: Dict[str, datetime] = {}
_BED_WATCHERS: Dict[str, asyncio.Task] = {}

def _mark_bed_empty(printer_id: str) -> None:
    pid = (printer_id or "-").strip().lower()
    if pid:
        _BED_EMPTY_TS[pid] = datetime.utcnow()

def _cancel_bed_watcher(printer_id: str) -> None:
    pid = (printer_id or "-").strip().lower()
    t = _BED_WATCHERS.pop(pid, None)
    if t and not t.done():
        t.cancel()

async def _start_bed_timeout_watcher(printer_id: str, finished_at: datetime, owner_emp: Optional[str]) -> None:
    pid = (printer_id or "-").strip().lower()
    try:
        deadline = finished_at + timedelta(seconds=WATCH_BED_EMPTY_TIMEOUT_SEC)
        while datetime.utcnow() < deadline:
            ts = _BED_EMPTY_TS.get(pid)
            if ts and ts > finished_at:
                return
            await asyncio.sleep(2.0)

        # NOTE: ใช้เว็บ/SSE เท่านั้น ไม่ DM
        if owner_emp:
            db2 = SessionLocal()
            try:
                mins = WATCH_BED_EMPTY_TIMEOUT_SEC // 60
                await notify_user(
                    db2, owner_emp,
                    type="print.pickup_required",
                    title="🧰 Please remove your print from the bed",
                    message=f"Printer {pid} • Still not cleared {mins} min after completion (Bangkok time { _fmt_bkk() })",
                    severity="warning",
                    data={"printer_id": pid, "timeout_sec": WATCH_BED_EMPTY_TIMEOUT_SEC}
                )
            finally:
                try: db2.close()
                except Exception: pass
    except asyncio.CancelledError:
        return
    except Exception:
        log.exception("[BED-WATCHER] error pid=%s", pid)

# =============================================================================
# SSE broker (per-employee)
# =============================================================================
class NotificationBroker:
    def __init__(self):
        self.subscribers: Dict[str, Set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, emp: str) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=200)
        async with self._lock:
            self.subscribers.setdefault(emp, set()).add(q)
        return q

    def unsubscribe(self, emp: str, q: asyncio.Queue):
        qs = self.subscribers.get(emp)
        if not qs: return
        qs.discard(q)
        if not qs: self.subscribers.pop(emp, None)

    async def publish(self, emp: str, payload: dict):
        qs = list(self.subscribers.get(emp, set()))
        if not qs: return
        for q in qs:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                try: _ = q.get_nowait()
                except Exception: pass
                try: q.put_nowait(payload)
                except Exception: self.unsubscribe(emp, q)

broker = NotificationBroker()

# =============================================================================
# Helpers
# =============================================================================
def _json(data: dict) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return json.dumps({"bad_payload":"<unserializable>"}, ensure_ascii=False)

def _emp(x: Optional[str]) -> str:
    return str(x or "").trim()

# 👇 NEW: helper เลือก "คนที่ควรโดนแจ้งเตือน" จากงานพิมพ์
def _primary_emp_from_job(job: PrintJob) -> str:
    """
    ใช้ requested_by_employee_id เป็นหลัก (คนที่กดสั่งพิมพ์จริง)
    ถ้าไม่มี → fallback เป็น employee_id (เจ้าของไฟล์ / คนสร้างงาน)
    """
    try:
        requested = _emp(getattr(job, "requested_by_employee_id", None))
    except Exception:
        requested = ""
    owner = _emp(getattr(job, "employee_id", None))
    return requested or owner

def _to_out(n: Notification, read_at: Optional[datetime]) -> NotificationOut:
    # ✅ ป้องกันข้อมูลเก่าใน DB ที่มี severity แปลก ๆ (เช่น "neutral") ทำให้ Pydantic ล้ม
    sev = _normalize_severity_for_out(getattr(n, "severity", None))
    return NotificationOut(
        id=n.id,
        type=n.ntype,
        severity=sev,
        title=n.title,
        message=n.message,
        data=json.loads(n.data_json) if n.data_json else None,
        created_at=n.created_at,
        read=bool(read_at),
    )

def _rows_to_out(rows: Iterable[Tuple[Notification, Optional[datetime]]]) -> List[NotificationOut]:
    return [_to_out(n, read_at) for (n, read_at) in rows]

def _preview_key_from_gcode(gk: Optional[str]) -> Optional[str]:
    if not gk: return None
    gk = str(gk).strip()
    if not gk: return None
    if not re.search(r"\.(gcode|gco|gc)$", gk, flags=re.I): return None
    return re.sub(r"\.(gcode|gco|gc)$", ".preview.png", gk, flags=re.I)

def _valid_url(u: Optional[str]) -> bool:
    try:
        return bool(u and re.match(r"^https?://", u.strip(), flags=re.I))
    except Exception:
        return False

def _build_safe_url(url_in: Optional[str], *, job_id: Optional[int|str] = None) -> Optional[str]:
    if _valid_url(url_in):
        return url_in.strip()
    base = PUBLIC_BASE_URL or FRONTEND_BASE_URL
    if _valid_url(base) and job_id:
        return f"{base}/#/printing?job_id={job_id}"
    if _valid_url(base):
        return f"{base}/#/printing"
    return None

def _as_int_or_zero(v: Any) -> int:
    try:
        return int(v)
    except Exception:
        return 0

# === Active job helper ========================================================
def _find_active_job(db: Session, printer_id: str) -> Optional[PrintJob]:
    """คืนงานที่กำลังอยู่บนเตียง (processing/printing/paused) ล่าสุดของเครื่องนั้น"""
    pid = (printer_id or DEFAULT_PRINTER_ID or "-").strip().lower()
    return (db.query(PrintJob)
              .filter(PrintJob.printer_id == pid,
                      PrintJob.status.in_(("processing","printing","paused")))
              .order_by(PrintJob.started_at.desc().nullslast(), PrintJob.id.desc())
              .first())

# =============================================================================
# Canonical event format helper
# =============================================================================
def format_canonical_event(
    *, type: Optional[str]=None, status: Optional[str]=None, severity: Optional[str]=None,
    title: Optional[str]=None, message: Optional[str]=None,
    printer_id: Optional[str]=None, data: Optional[dict]=None,
    created_at: Optional[str]=None, read: bool=False,
) -> dict:
    st = _canon_status(status)
    t = _canon_type(type, st)
    sev = _norm_severity_from_type(t, severity)
    d = dict(data or {})
    if printer_id and not str(d.get("printer_id") or "").strip():
        d["printer_id"] = printer_id
    nm = (d.get("name") or d.get("job_name") or d.get("filename") or d.get("file") or "").strip()
    if nm:
        d.setdefault("name", nm)
        d.setdefault("job_name", nm)
    if st and not str(d.get("status") or "").strip():
        d["status"] = st
    if not message:
        pr = d.get("printer_id") or printer_id or ""
        name_txt = f"“{nm}” " if nm else ""
        on_txt = f" on {pr}" if pr else ""
        message = {
            "print.queued":     f"{name_txt}entered the queue{on_txt}.",
            "print.started":    f"{name_txt}is now starting{on_txt}.",
            "print.processing": f"{name_txt}is processing{on_txt}.",
            "print.completed":  f"{name_txt}finished{on_txt}.",
            "print.failed":     f"{name_txt}failed{on_txt}.",
            "print.canceled":   f"{name_txt}was canceled{on_txt}.",
            "print.paused":     f"{name_txt}has been paused{on_txt}.",
            "print.issue":      f"Issue detected {name_txt}{on_txt}.",
            "print.pickup_required": f"{name_txt}needs pickup{on_txt}.",
        }.get(t, f"{name_txt}updated{on_txt}.")
    default_titles = {
        "print.queued":"Queued","print.started":"Print started","print.processing":"Processing",
        "print.completed":"Print completed","print.failed":"Print failed",
        "print.canceled":"Print canceled","print.paused":"Print paused","print.issue":"Printer issue",
        "print.pickup_required":"Pickup required",
    }
    ttl = title or default_titles.get(t) or "Notification"
    return {
        "type": t, "severity": sev, "title": ttl, "message": message,
        "printer_id": printer_id, "data": d,
        "created_at": created_at or datetime.utcnow().isoformat(), "read": bool(read),
    }

async def emit_canonical_event(db: Session, employee_id: str, ev: dict) -> None:
    try:
        await notify_user(db, employee_id, payload=ev)
    except Exception:
        log.exception("[emit] notify_user failed")
    try:
        await _notify_job_event_from_canonical(db, payload=ev)
    except Exception:
        log.exception("[emit] notify_job_event canonical failed")

# =============================================================================
# Channels: Email / Teams DM / HoloLens / SSE
# =============================================================================
def _send_email_bg(emp_id: str, ntype: str, title: str, message: str | None, data: dict | None):
    if not EMAIL_ENABLED:
        return
    db2 = SessionLocal()
    try:
        msg = (message or "")
        if "Bangkok time" not in msg:
            msg = f"{msg}  (Bangkok time { _fmt_bkk() })"
        send_notification_email(db2, emp_id, ntype=ntype, title=title or "", message=msg, data=data or None)
    except Exception:
        log.exception("[NOTIFY] email bg failed")
    finally:
        try: db2.close()
        except Exception: pass

def _dm_env_ready() -> bool:
    url = _clean_env(os.getenv("FLOW_DM_URL"))
    tok = _clean_env(os.getenv("FLOW_DM_TOKEN"))
    if not url:
        log.warning("[DM] FLOW_DM_URL is empty — DM will be skipped")
        return False
    if not tok:
        log.warning("[DM] FLOW_DM_TOKEN is empty — DM may fail if flow requires it")
    return True

_DM_SUPPRESS_TTL_SEC = int(_as_float(os.getenv("DM_SUPPRESS_TTL_SEC"), 20))
_recent_dm: Dict[Tuple[str,str], datetime] = {}

def _dm_should_skip_dup(emp_id: str, status: str) -> bool:
    now = datetime.utcnow()
    k = (emp_id.strip(), (status or "").strip().lower())
    ts = _recent_dm.get(k)
    if ts and (now - ts).total_seconds() < _DM_SUPPRESS_TTL_SEC:
        return True
    _recent_dm[k] = now
    if len(_recent_dm) > 500:
        cutoff = now - timedelta(seconds=_DM_SUPPRESS_TTL_SEC*2)
        for kk, vv in list(_recent_dm.items()):
            if vv < cutoff: _recent_dm.pop(kk, None)
    return False

# ==== DM gate (policy check) =================================================
def _should_send_dm_by_policy(ntype: str, status: str | None, data: dict | None) -> tuple[bool, str]:
    """
    คืน (allowed, reason) — จะส่ง DM เฉพาะเมื่อผ่าน policy:
      - DM_ENABLED = true
      - ชนิดอยู่ใน DM_FOR_TYPES_SET (ยอมรับทั้ง 'queued' และ 'print.queued')
      - (เมื่อ DM_REQUIRE_JOB_ID) ต้องมี job_id > 0  **ยกเว้น** print.issue
      - (เมื่อ DM_REQUIRE_PRINTER_ID) ต้องมี printer_id
    """
    if not DM_ENABLED:
        return (False, "dm_disabled")

    t = (ntype or "").strip().lower()
    st = _canon_status(status)

    if (t not in DM_FOR_TYPES_SET) and (st not in DM_FOR_TYPES_SET):
        return (False, f"type_not_allowed:{st or t}")

    d = dict(data or {})
    job_id = 0
    try:
        job_id = int(d.get("job_id") or 0)
    except Exception:
        job_id = 0
    printer_id = (d.get("printer_id") or "").strip().lower()

    require_job = DM_REQUIRE_JOB_ID
    if (t == "print.issue") or (st == "issue"):
        require_job = False  # relax for issue

    if require_job and job_id <= 0:
        return (False, "missing_job_id")

    if DM_REQUIRE_PRINTER_ID and (not printer_id or printer_id == "-"):
        return (False, "missing_printer_id")

    return (True, "ok")

def _send_dm_bg(emp_id: str, ntype: str, title: str, message: str | None, data: dict | None):
    """
    ส่ง DM ไป Power Automate โดยฟิลด์ต้องครบตาม policy;
    จะพยายาม backfill job_id สำหรับ issue ถ้ามีแต่ printer_id
    """
    if not _dm_env_ready():
        return

    d = dict(data or {})
    status_for_card = _canon_status(d.get("status") or (ntype.split(".",1)[-1] if "." in (ntype or "") else ""))

    # ---- backfill job_id สำหรับ issue ถ้าขาดแต่มี printer_id ----
    if (status_for_card == "issue" or ntype == "print.issue") and int(d.get("job_id") or 0) <= 0:
        pid = (d.get("printer_id") or "").strip().lower()
        if pid:
            try:
                dbx = SessionLocal()
                try:
                    job = (dbx.query(PrintJob)
                             .filter(PrintJob.printer_id == pid,
                                     PrintJob.status.in_(("processing","printing","paused")))
                             .order_by(PrintJob.started_at.desc().nullslast(), PrintJob.id.desc())
                             .first())
                    if job:
                        d["job_id"] = int(job.id)
                        d.setdefault("name", job.name)
                        d.setdefault("job_name", job.name)
                        d.setdefault("status", status_for_card or "issue")
                finally:
                    dbx.close()
            except Exception:
                log.exception("[DM] backfill job_id failed")

    allowed, reason = _should_send_dm_by_policy(ntype, status_for_card, d)
    if not allowed:
        log.info("[DM] skip by policy: emp=%s type=%s reason=%s", emp_id, ntype, reason)
        return

    # --- lookup user's email ---
    db2 = SessionLocal()
    employee_email = ""
    try:
        u = db2.query(User).filter(User.employee_id == (emp_id or "").strip()).first()
        employee_email = (u.email or "").strip() if u else ""
    except Exception:
        log.exception("[DM] lookup user email failed")
    finally:
        try: db2.close()
        except Exception: pass

    if not employee_email:
        log.warning("[DM] skip: employee %s has no email", emp_id)
        return

    job_id   = _as_int_or_zero(d.get("job_id"))
    printer_id = (d.get("printer_id") or "").strip().lower() or "-"
    job_name = (d.get("name") or d.get("job_name") or d.get("filename") or "-").strip()

    if _dm_should_skip_dup(emp_id, status_for_card):
        log.info("[DM] suppressed duplicate recently emp=%s status=%s", emp_id, status_for_card)
        return

    url_safe = _build_safe_url(d.get("url"), job_id=job_id if job_id else None)

    title_to_send = DM_TITLE
    msg = (message or "")
    if "Bangkok time" not in msg:
        msg = f"{msg}  (Bangkok time { _fmt_bkk() })"

    delays = [0.0, 1.0, 2.0]
    last_exc = None
    for i, delay in enumerate(delays, 1):
        if delay: time.sleep(delay)
        try:
            notify_dm(
                employee_email=employee_email,
                status=status_for_card or "-",
                job_name=job_name or "-",
                printer_id=printer_id or "-",
                job_id=job_id or 0,
                title=title_to_send,
                message=msg,
                url=url_safe,
            )
            if i > 1:
                log.info("[DM] sent OK after retry #%d -> %s", i-1, employee_email)
            return
        except Exception as e:
            last_exc = e
            log.exception("[DM] attempt %d failed", i)
    if last_exc:
        log.error("[DM] failed after retries for %s", employee_email)

async def _mirror_to_hololens_if_possible(printer_id: Optional[str], *, severity: str, title: str, message: str, extra_ui: dict | None = None):
    if not HOLOLENS_MIRROR_USER_NOTIFS:
        return
    pid = (printer_id or "").strip().lower()
    if not pid:
        return
    ui = {"type": "toast", "timeout_ms": 6000}
    if HOLOLENS_MIRROR_EVENT_NAME == "alert":
        ui = {"type": "panel", "variant": severity, "sticky": False, "timeout_ms": 6000}
    if extra_ui:
        ui.update(extra_ui)

    payload = {
        "event": HOLOLENS_MIRROR_EVENT_NAME,
        "printer_id": pid,
        "ts": datetime.utcnow().timestamp(),
        "severity": severity,
        "title": title,
        "message": message or "",
        "ui": ui,
    }
    try:
        await _emit_printer_event(pid, payload)
    except Exception:
        log.exception("[HoloMirror] emit error")

# =============================================================================
# notify_user — single place: DB/SSE + DM + Email + HoloLens
# =============================================================================
async def notify_user(
    db: Session,
    employee_id: str,
    *,
    type: str | None = None,
    title: str | None = None,
    message: str | None = None,
    severity: str = "info",
    data: dict | None = None,
    payload: dict | None = None,
    **extra: Any,
) -> Notification:
    # merge from payload if provided
    if payload and not type and not title:
        try:
            type = payload.get("type") or type
            title = payload.get("title") or title
            message = payload.get("message") if message is None else message
            severity = payload.get("severity") or severity
            base_data = dict(payload.get("data") or {})
            if data: base_data.update(data)
            data = base_data
        except Exception:
            pass

    # normalize type/status/severity BEFORE persisting/DM
    d_safe = dict(data or {})
    st_in = _canon_status(d_safe.get("status") or None)
    type_canon = _canon_type(type, st_in)
    sev_final = _norm_severity_from_type(type_canon, severity)

    if not st_in:
        if "." in (type_canon or ""):
            st_in = (type_canon or "").split(".", 1)[1]
    if st_in:
        d_safe["status"] = st_in

    n = Notification(
        ntype=type_canon or (type or "notification"),
        title=title or (extra.get("title") or "Notification"),
        message=message if message is not None else extra.get("message"),
        severity=sev_final,
        data_json=json.dumps(d_safe, ensure_ascii=False)
    )
    db.add(n); db.commit(); db.refresh(n)
    db.add(NotificationTarget(notification_id=n.id, employee_id=employee_id)); db.commit()

    event = {
        "id": n.id, "type": n.ntype, "severity": n.severity, "title": n.title, "message": n.message,
        "data": json.loads(n.data_json) if n.data_json else None, "created_at": n.created_at.isoformat(), "read": False
    }
    await broker.publish(employee_id, event)

    payload_for_channels = json.loads(n.data_json) if n.data_json else None

    _spawn(_send_dm_bg,   employee_id, n.ntype, n.title or "", n.message, payload_for_channels)
    _spawn(_send_email_bg, employee_id, n.ntype, n.title or "", n.message, payload_for_channels)

    try:
        d = payload_for_channels or {}
        pid = (d.get("printer_id") or (d.get("printer") or "")).strip()
        await _mirror_to_hololens_if_possible(pid, severity=n.severity, title=n.title or "", message=n.message or "", extra_ui=None)
    except Exception:
        log.exception("[notify_user] hololens mirror failed")

    return n

async def notify_many(db: Session, employee_ids: List[str], **kwargs):
    for emp in employee_ids[:1000]:
        await notify_user(db, emp, **kwargs)

# =============================================================================
# Legacy/canonical bridge for job events
# =============================================================================
async def _notify_job_event_from_canonical(db: Session, payload: dict | None = None) -> Any:
    try:
        t = (payload or {}).get("type", "")
        log.info("[notify_job_event/canonical] passthrough type=%s", t)
    except Exception:
        pass
    class _Dummy: ...
    d = _Dummy()
    setattr(d, "id", 0)
    setattr(d, "ntype", (payload or {}).get("type") if isinstance(payload, dict) else "print.event")
    setattr(d, "title", (payload or {}).get("title") if isinstance(payload, dict) else "")
    setattr(d, "message", (payload or {}).get("message") if isinstance(payload, dict) else "")
    setattr(d, "severity", (payload or {}).get("severity") if isinstance(payload, dict) else "info")
    setattr(d, "created_at", datetime.utcnow())
    return d

async def notify_job_event(
    db: Session,
    job_or_payload: Union[PrintJob, dict],
    status_or_mode: Optional[str] = None,
    *,
    title: Optional[str] = None,
    message: Optional[str] = None,
    severity: Optional[str] = None,
    data: Optional[dict] = None,
    payload: Optional[dict] = None,
) -> Any:
    if isinstance(job_or_payload, dict) or (status_or_mode == "canonical") or payload is not None:
        return await _notify_job_event_from_canonical(db, payload=job_or_payload if isinstance(job_or_payload, dict) else payload)

    job: PrintJob = job_or_payload  # type: ignore[assignment]
    status = _canon_status((status_or_mode or "").strip().lower())
    # 👇 ใช้ primary recipient จาก requested_by → fallback employee_id
    recipient = _primary_emp_from_job(job)

    type_str = _canon_type(f"print.{status}" if status else "print.issue", status)
    if title is None:
        title_map = {
            "queued": "Added to print queue",
            "processing": "Your job is starting",
            "completed": "Print completed",
            "failed": "Print failed",
            "canceled": "Print canceled",  # แก้ข้อความให้ถูกต้อง
            "paused": "Print paused",
        }
        title = title_map.get(status, f"Print status: {status or '-'}")
    if severity is None:
        severity = {"completed":"success","failed":"error","canceled":"warning"}.get(status,"info")
    if message is None:
        message = job.name

    gk = (getattr(job, "gcode_path", None) or getattr(job, "gcode_key", None) or "").strip() or None
    payload2 = dict(data or {})
    payload2.setdefault("job_id", job.id)
    payload2.setdefault("printer_id", job.printer_id)
    payload2.setdefault("name", job.name)
    payload2.setdefault("employee_id", _emp(job.employee_id))
    payload2.setdefault("requested_by_employee_id", _emp(getattr(job, "requested_by_employee_id", None)))
    payload2.setdefault("gcode_key", gk)
    payload2.setdefault("preview_key", _preview_key_from_gcode(gk))
    payload2.setdefault("status", status)

    return await notify_user(
        db, recipient,
        type=type_str, title=title, message=message, severity=severity, data=payload2
    )

# =============================================================================
# WebSocket for Unity/HoloLens (per-printer “room”)
# =============================================================================
class UnityAlertHub:
    def __init__(self):
        self.rooms: dict[str, set[WebSocket]] = {}

    async def connect(self, ws: WebSocket, printer_id: str):
        await ws.accept()
        self.rooms.setdefault(printer_id, set()).add(ws)

    def disconnect(self, ws: WebSocket, printer_id: str):
        room = self.rooms.get(printer_id)
        if room:
            room.discard(ws)
            if not room: self.rooms.pop(printer_id, None)
        star = self.rooms.get("*", set())
        if ws in star:
            star.discard(ws)
            if not star: self.rooms.pop("*", None)

    async def broadcast(self, printer_id: str, payload: dict):
        targets = set()
        if printer_id in self.rooms: targets |= self.rooms[printer_id]
        if "*" in self.rooms: targets |= self.rooms["*"]

        dead: list[WebSocket] = []
        data = json.dumps(payload, ensure_ascii=False)
        for ws in list(targets):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)

        if dead:
            for key, room in list(self.rooms.items()):
                for ws in dead:
                    if ws in room: room.discard(ws)
                if not room: self.rooms.pop(key, None)

unity_ws = UnityAlertHub()

@router.websocket("/ws/alerts")
async def ws_alerts(websocket: WebSocket, printer_id: str | None = Query(default="*")):
    pid = (printer_id or "*").strip().lower()
    await unity_ws.connect(websocket, pid)
    try:
        while True:
            _ = await websocket.receive_text()
    except WebSocketDisconnect:
        unity_ws.disconnect(websocket, pid)

# =============================================================================
# FE (web) per-printer SSE — same events as HoloLens
# =============================================================================
class PrinterSSEBroker:
    def __init__(self):
        self.subscribers: Dict[str, Set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, printer_id: str) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=500)
        pid = (printer_id or "*").strip().lower()
        async with self._lock:
            self.subscribers.setdefault(pid, set()).add(q)
        return q

    def unsubscribe(self, printer_id: str, q: asyncio.Queue):
        pid = (printer_id or "*").strip().lower()
        qs = self.subscribers.get(pid)
        if not qs: return
        qs.discard(q)
        if not qs: self.subscribers.pop(pid, None)

    async def publish(self, printer_id: str, payload: dict):
        targets = []
        for key in ((printer_id or "").strip().lower(), "*"):
            for q in list(self.subscribers.get(key, set())):
                targets.append((key, q))
        dead = []
        for key, q in targets:
            try:
                await q.put(payload)
            except Exception:
                dead.append((key, q))
        for key, q in dead:
            self.unsubscribe(key, q)

printer_sse = PrinterSSEBroker()

async def _emit_printer_event(printer_id: str, payload: dict):
    _spawn(printer_sse.publish, printer_id, payload)
    await unity_ws.broadcast(printer_id, payload)

@router.get("/printers/stream")
async def printers_stream(
    request: Request,
    user: User = Depends(get_user_from_header_or_query),
    printer_id: str = Query(default="*")
):
    pid = (printer_id or "*").strip().lower()
    q = await printer_sse.subscribe(pid)

    async def gen():
        yield ":connected\n\n"
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    if await request.is_disconnected(): break
                    yield ":keepalive\n\n"
        finally:
            printer_sse.unsubscribe(pid, q)

    headers = {
        "Cache-Control":"no-cache", "X-Accel-Buffering":"no", "Connection":"keep-alive",
        "Access-Control-Allow-Origin":"*",
    }
    return StreamingResponse(gen(), media_type="text/event-stream; charset=utf-8", headers=headers)

# =============================================================================
# Detects ring buffer + SSE (system-level)
# =============================================================================
RECENT_DETECT_MAX = int(_as_float(os.getenv("RECENT_DETECT_MAX"), 500))
RECENT_DETECTS = deque(maxlen=max(50, RECENT_DETECT_MAX))

class DetectRecord(BaseModel):
    ts: float
    event: str
    printer_id: str | None = None
    detected_class: str | None = None
    confidence: float | None = None
    image_url: str | None = None
    video_url: str | None = None
    boxes: list[list[float]] | None = None
    image_w: int | None = None
    image_h: int | None = None
    source: str | None = None

def _push_detect(payload: dict):
    try:
        RECENT_DETECTS.append(DetectRecord(
            ts=float(payload.get("ts") or 0),
            event=str(payload.get("event") or ""),
            printer_id=payload.get("printer_id"),
            detected_class=payload.get("detected_class"),
            confidence=payload.get("confidence"),
            image_url=payload.get("image_url"),
            video_url=payload.get("video_url"),
            boxes=payload.get("boxes"),
            image_w=payload.get("image_w"),
            image_h=payload.get("image_h"),
            source=payload.get("source"),
        ))
    except Exception:
        pass

class DetectSSEBroker:
    def __init__(self): self.subs: set[asyncio.Queue] = set()
    async def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=500); self.subs.add(q); return q
    def unsubscribe(self, q: asyncio.Queue): self.subs.discard(q)
    async def publish(self, payload: dict):
        dead=[]
        for q in list(self.subs):
            try: await q.put(payload)
            except Exception: dead.append(q)
        for q in dead: self.unsubscribe(q)

detect_broker = DetectSSEBroker()

# ---- Debounce confirm state ----
_PENDING_ISSUES: dict[tuple[str,str], dict] = {}
def _norm(s: str) -> str: return (s or "").strip().lower().replace("-", "_").replace("  "," ").replace(" ","_")
def _norm_cls(s: Optional[str]) -> str: return _norm(s or "")
def _pend_key(printer_id: str, clsname: str) -> tuple[str,str]:
    return ((printer_id or "-").strip().lower(), (clsname or "").strip().lower())

def _now_ts_fallback(ts: Optional[float]) -> float:
    try:
        t = float(ts or 0.0); return t if t > 0 else datetime.utcnow().timestamp()
    except Exception:
        return datetime.utcnow().timestamp()

def _add_pending_hit(printer_id: str, clsname: str, *, ts: float, conf: float, payload: dict):
    k = _pend_key(printer_id, clsname)
    st = _PENDING_ISSUES.get(k) or {"first_ts": ts, "last_ts": ts, "hits": 0, "sum_conf": 0.0, "max_conf": 0.0, "last_payload": None}
    st["hits"] += 1; st["last_ts"] = ts; st["sum_conf"] += float(conf or 0.0); st["max_conf"] = max(st["max_conf"], float(conf or 0.0))
    st["last_payload"] = payload
    _PENDING_ISSUES[k] = st
    return st

def _clear_pending(printer_id: str, clsname: str): _PENDING_ISSUES.pop(_pend_key(printer_id, clsname), None)

def _check_confirm(st: dict) -> tuple[bool, float]:
    dt = st["last_ts"] - st["first_ts"]; hits = st["hits"]
    mean_conf = (st["sum_conf"]/hits) if hits>0 else 0.0
    return (
        dt >= DETECT_CONFIRM_WINDOW_SEC
        and hits >= DETECT_CONFIRM_MIN_HITS
        and mean_conf >= DETECT_CONFIRM_MIN_MEAN_CONF
    ), mean_conf

def _gc_pending():
    cutoff = datetime.utcnow().timestamp() - (2.0 * max(1.0, DETECT_CONFIRM_WINDOW_SEC))
    for k, st in list(_PENDING_ISSUES.items()):
        if st.get("last_ts", 0) < cutoff:
            _PENDING_ISSUES.pop(k, None)

# =============================================================================
# Sticky alert watchdog (HoloLens panel)
# =============================================================================
_PAUSE_PANEL_ON: Dict[str, bool] = {}
async def _panel_watchdog(printer_id: str, payload: dict, interval: float = 12.0):
    pid = (printer_id or "-").strip().lower()
    _PAUSE_PANEL_ON[pid] = True
    try:
        await _emit_printer_event(pid, payload)
        while _PAUSE_PANEL_ON.get(pid, False):
            await asyncio.sleep(interval)
            await _emit_printer_event(pid, payload)
    except Exception:
        log.exception("[PANEL] watchdog error")
    finally:
        _PAUSE_PANEL_ON[pid] = False

def _stop_panel_watchdog(printer_id: str): _PAUSE_PANEL_ON[(printer_id or "-").strip().lower()] = False

async def _close_sticky_panel(printer_id: str, persist_key: str):
    _stop_panel_watchdog(printer_id)
    try:
        await _emit_printer_event(printer_id, {
            "event":"alert_close", "printer_id":printer_id, "persist_key":persist_key, "ts":datetime.utcnow().timestamp()
        })
    except Exception:
        log.exception("[PANEL] close panel error")

# =============================================================================
# Octo pause helpers
# =============================================================================
async def _pause_octoprint() -> None:
    if not _octo_ready():
        log.warning("[PAUSE] OctoPrint not configured; skip")
        return
    url = f"{OCTO_BASE}/api/job"
    payload = {"command":"pause","action":"pause"}
    timeout = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, headers={**_octo_headers(),"Content-Type":"application/json"}, json=payload)
            log.info("[PAUSE] POST %s -> %s %s", url, r.status_code, r.text[:200]); r.raise_for_status()
    except httpx.HTTPError:
        log.exception("[PAUSE] HTTPError")
    except Exception:
        log.exception("[PAUSE] unexpected error")

def _pause_current_job_in_db(db: Session, printer_id: str) -> Optional[PrintJob]:
    pid = (printer_id or DEFAULT_PRINTER_ID or "-").strip().lower()
    job = (db.query(PrintJob)
             .filter(PrintJob.printer_id == pid, PrintJob.status.in_(("processing","printing")))
             .order_by(PrintJob.started_at.desc(), PrintJob.id.desc())
             .first())
    if not job: return None
    job.status = "paused"; db.add(job); db.commit(); db.refresh(job)
    return job

async def _backend_read_job(printer_id: str) -> dict:
    pid = (printer_id or DEFAULT_PRINTER_ID or "-").strip().lower()
    url = f"{BACKEND_INTERNAL_BASE}/printers/{pid}/octoprint/job?force=1"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url); r.raise_for_status(); return r.json()
    except Exception:
        return {}

# =============================================================================
# Dedupe/suppress (job events)
# =============================================================================
JOB_EVENT_DEDUP_TTL_SEC = int(_as_float(os.getenv("JOB_EVENT_DEDUP_TTL_SEC"), 15))
_recent_job_events: Dict[str, datetime] = {}

def _dupkey(printer_id: str, job_id: Union[int,str,None], status: str) -> str:
    return f"{(printer_id or '').lower()}|{job_id or '-'}|{(status or '').lower()}"

def _should_skip_job_event(printer_id: str, job_id: Union[int,str,None], status: str) -> bool:
    now = datetime.utcnow(); k = _dupkey(printer_id, job_id, status); ts = _recent_job_events.get(k)
    if ts and (now - ts).total_seconds() < JOB_EVENT_DEDUP_TTL_SEC: return True
    _recent_job_events[k] = now
    if len(_recent_job_events) > 500:
        cutoff = now - timedelta(seconds=JOB_EVENT_DEDUP_TTL_SEC*2)
        for kk, vv in list(_recent_job_events.items()):
            if vv < cutoff: _recent_job_events.pop(kk, None)
    return False

ANNOUNCE_TTL_HOURS = int(_as_float(os.getenv("ANNOUNCE_TTL_HOURS"), 12))
ANNOUNCED_JOB_STATUS: Dict[Tuple[str,int,str], datetime] = {}
def _announced(printer_id: str, job_id: Optional[int], status: str) -> bool:
    if not job_id: return False
    k = ((printer_id or "").lower(), int(job_id), (status or "").lower())
    ts = ANNOUNCED_JOB_STATUS.get(k)
    if not ts: return False
    if (datetime.utcnow() - ts).total_seconds() > ANNOUNCE_TTL_HOURS*3600:
        ANNOUNCED_JOB_STATUS.pop(k, None); return False
    return True
def _mark_announced(printer_id: str, job_id: Optional[int], status: str):
    if not job_id: return
    k = ((printer_id or "").lower(), int(job_id), (status or "").lower())
    ANNOUNCED_JOB_STATUS[k] = datetime.utcnow()

SUPPRESS_AFTER_CANCEL_SEC = int(_as_float(os.getenv("SUPPRESS_AFTER_CANCEL_SEC"), 25))
_SUPPRESS_UNTIL: Dict[str, datetime] = {}
def _suppress_after_cancel(printer_id: str, seconds: Optional[int] = None) -> None:
    sec = int(seconds or SUPPRESS_AFTER_CANCEL_SEC); pid = (printer_id or "").strip().lower()
    if not pid or sec <= 0: return
    _SUPPRESS_UNTIL[pid] = datetime.utcnow() + timedelta(seconds=sec)
def _is_suppressed(printer_id: str) -> Optional[str]:
    pid = (printer_id or "").strip().lower(); until = _SUPPRESS_UNTIL.get(pid)
    if not until: return None
    now = datetime.utcnow()
    if now >= until: _SUPPRESS_UNTIL.pop(pid, None); return None
    return until.isoformat()

# =============================================================================
# Schemas
# =============================================================================
Box = Tuple[float,float,float,float]

class DetectAlertIn(BaseModel):
    event: Literal["issue_started","issue_update","issue_cleared","issue_resolved"]
    ts: float
    image_url: Optional[str] = None
    video_url: Optional[str] = None
    boxes: List[List[float]] = []
    image_w: Optional[int] = None
    image_h: Optional[int] = None
    printer_id: Optional[str] = None
    source: Optional[str] = "detect_stream"
    recipients: Optional[List[str]] = None
    severity: Optional[Literal["info","warning","critical"]] = None
    detected_class: Optional[str] = None
    confidence: Optional[float] = None
    status_text: Optional[str] = None

ALLOWED_FAIL_DETECT_CLASSES = ALLOWED_DETECT_CLASSES

class JobEventIn(BaseModel):
    job_id: int
    status: Literal["completed","cancelled","failed"]
    printer_id: Optional[str] = None
    name: Optional[str] = None
    detected_class: Optional[str] = None
    confidence: Optional[float] = None
    finished_at: Optional[datetime] = None

def _status_title_severity(st: str) -> Tuple[str,str]:
    if st == "completed": return "🎉 Print completed","success"
    if st == "cancelled": return "🚫 Print cancelled","warning"
    return "❌ Print failed","critical"

def _auto_severity(event: str, conf: float | None) -> str:
    e = (event or "").lower().strip(); c = float(conf or 0.0)
    if e == "issue_started": return "critical" if c >= 0.80 else "warning"
    if e == "issue_update":  return "warning"  if c >= 0.70 else "info"
    return "info"

def _parse_employees_csv(s: str | None) -> list[str]:
    return [tok.strip() for tok in (s or "").split(",") if tok.strip()]

DEFAULT_ALERT_RECIPIENTS: list[str] = _parse_employees_csv(os.getenv("ALERT_DEFAULT_EMPLOYEES"))

def _find_active_owner(db: Session, printer_id: str) -> Optional[str]:
    """
    ตอนนี้ตีความว่า 'owner' = คนที่ควรถูกแจ้งเตือนหลักของงานบนเตียง
    (requested_by_employee_id ถ้ามี, ถ้าไม่มีค่อย fallback employee_id)
    """
    pid = (printer_id or DEFAULT_PRINTER_ID or "-").strip().lower()
    job = (db.query(PrintJob)
             .filter(PrintJob.printer_id == pid, PrintJob.status.in_(("processing","printing","paused")))
             .order_by(PrintJob.started_at.desc().nullslast(), PrintJob.id.desc())
             .first())
    if not job:
        return None
    return _primary_emp_from_job(job)

def _normalize_recipients(recipients: list[str] | None) -> list[str]:
    return [ (r or "").strip() for r in (recipients or []) if (r or "").strip() ]

# ---- Reason helper (pretty label) -------------------------------------------
_REASON_LABELS = {
    "spaghetti": "Spaghetti",
    "layer_shift": "Layer shift",
    "stringing": "Stringing",
    "cracks": "Cracks",
}
def _reason_label(clsname: str) -> str:
    c = (clsname or "").strip().lower()
    return _REASON_LABELS.get(c, c or "-")

# =============================================================================
# REST — per-user notifications (web)
# =============================================================================
@router.get("", response_model=List[NotificationOut])
def list_notifications(
    limit: int = 20,
    db: Session = Depends(get_db),
    current: User = Depends(get_user_from_header_or_query),
):
    limit = max(1, min(int(limit or 20), 100))
    try:
        q = (db.query(Notification, NotificationTarget.read_at)
               .join(NotificationTarget, Notification.id == NotificationTarget.notification_id)
               .filter(NotificationTarget.employee_id == current.employee_id)
               .order_by(Notification.id.desc())
               .limit(limit))
        return _rows_to_out(q.all())
    except Exception:
        return []

@router.post("", response_model=List[NotificationOut], status_code=status.HTTP_201_CREATED)
async def create_notification(
    payload: NotificationCreate,
    db: Session = Depends(get_db),
    current: User = Depends(get_user_from_header_or_query),
):
    recipients = (payload.recipients or [current.employee_id])[:200]
    out: List[NotificationOut] = []
    for emp in recipients:
        n = await notify_user(
            db, emp, type=payload.type, title=payload.title,
            message=payload.message, severity=payload.severity, data=payload.data or {}
        )
        out.append(_to_out(n, read_at=None))
    return out

@router.post("/mark-read")
def mark_read(
    payload: NotificationMarkRead,
    db: Session = Depends(get_db),
    current: User = Depends(get_user_from_header_or_query),
):
    if not payload.ids: return {"ok": True, "updated": 0}
    now = datetime.utcnow()
    updated = (db.query(NotificationTarget)
                 .filter(NotificationTarget.employee_id == current.employee_id,
                         NotificationTarget.notification_id.in_(payload.ids),
                         NotificationTarget.read_at.is_(None))
                 .update({NotificationTarget.read_at: now}, synchronize_session=False))
    db.commit()
    return {"ok": True, "updated": int(updated or 0)}

@router.post("/mark-all-read")
def mark_all_read(
    db: Session = Depends(get_db),
    current: User = Depends(get_user_from_header_or_query),
):
    updated = (db.query(NotificationTarget)
                 .filter(NotificationTarget.employee_id == current.employee_id,
                         NotificationTarget.read_at.is_(None))
                 .update({NotificationTarget.read_at: datetime.utcnow()}, synchronize_session=False))
    db.commit()
    return {"ok": True, "updated": int(updated or 0)}

@router.delete("/{notif_id}")
def delete_notification(
    notif_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_user_from_header_or_query),
):
    target = (db.query(NotificationTarget)
                .filter(NotificationTarget.employee_id == current.employee_id,
                        NotificationTarget.notification_id == notif_id)
                .first())
    if not target: raise HTTPException(status_code=404, detail="Not found")

    db.delete(target); db.commit()

    remaining = (db.query(NotificationTarget)
                   .filter(NotificationTarget.notification_id == notif_id)
                   .count())
    if remaining == 0:
        n = db.query(Notification).filter(Notification.id == notif_id).first()
        if n: db.delete(n); db.commit()
    return {"ok": True}

# =============================================================================
# SSE per-user stream (web) — backlog init_limit
# =============================================================================
@router.get("/stream")
async def stream(
    request: Request,
    user: User = Depends(get_user_from_header_or_query),
    init_limit: int = Query(default=0, ge=0, le=100, description="Initial backlog count"),
):
    emp = str(user.employee_id)
    q = await broker.subscribe(emp)

    async def gen():
        try:
            yield "retry: 3000\n\n"
            yield ":connected\n\n"

            if init_limit > 0:
                db2 = SessionLocal()
                try:
                    rows = (db2.query(Notification, NotificationTarget.read_at)
                              .join(NotificationTarget, Notification.id == NotificationTarget.notification_id)
                              .filter(NotificationTarget.employee_id == emp)
                              .order_by(Notification.id.desc())
                              .limit(init_limit)
                              .all())
                    for item in reversed(_rows_to_out(rows)):
                        payload = item.model_dump(mode="json")
                        yield f'event: backlog\ndata: {_json(payload)}\n\n'
                finally:
                    db2.close()

            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {_json(event)}\n\n"
                except asyncio.TimeoutError:
                    if await request.is_disconnected(): break
                    yield ":keepalive\n\n"
        finally:
            broker.unsubscribe(emp, q)

    headers = {
        "Cache-Control":"no-cache", "X-Accel-Buffering":"no", "Connection":"keep-alive",
        "Access-Control-Allow-Origin":"*",
    }
    return StreamingResponse(gen(), media_type="text/event-stream; charset=utf-8", headers=headers)

# =============================================================================
# Detect stream ingest (camera/AI)
# =============================================================================
@router.post("/alerts")
async def ingest_detect_alert(
    payload: DetectAlertIn,
    db: Session = Depends(get_db),
    x_admin_token: str = Header(default="")
):
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    log.info("[ALERT] recv %s", payload.model_dump_json(indent=0))

    # ===== LATENCY: camera -> backend ingest =====
    try:
        src_ts = float(payload.ts or 0.0)             # เวลา ณ RasPi (ts จาก detect_stream)
    except Exception:
        src_ts = 0.0

    now_ts = datetime.utcnow().timestamp()            # เวลา ณ backend ตอนรับ request
    lat_cam_to_backend = max(0.0, now_ts - src_ts) if src_ts > 0 else 0.0

    # confidence ไว้ใช้ทั้ง log latency + ด้านล่าง
    conf = float(payload.confidence or 0.0)

    # --- เก็บ log latency กล้อง -> backend ลง DB ---
    if src_ts > 0:
        _log_latency_row(
            db,
            channel="backend",
            path="/notifications/alerts",
            t_send_epoch=src_ts,
            note=(
                f"event={payload.event} "
                f"printer={(payload.printer_id or '-')} "
                f"cls={(payload.detected_class or '?')} "
                f"conf={conf:.2f}"
            ),
        )

    log.info(
        "[LAT] ingest printer=%s event=%s cls=%s lat_cam_to_backend=%.3fs",
        (payload.printer_id or DEFAULT_PRINTER_ID or "-").strip().lower(),
        payload.event,
        (payload.detected_class or "?"),
        lat_cam_to_backend,
    )

    printer_id = (payload.printer_id or DEFAULT_PRINTER_ID or "-").strip().lower()
    clsname_raw = payload.detected_class or ""
    clsname = _norm_cls(clsname_raw)
    event = "issue_cleared" if payload.event == "issue_resolved" else payload.event

    base = {
        "ts": payload.ts, "event": event, "printer_id": printer_id,
        "detected_class": payload.detected_class, "confidence": payload.confidence,
        "image_url": payload.image_url, "video_url": payload.video_url,
        "boxes": payload.boxes, "image_w": payload.image_w, "image_h": payload.image_h,
        "source": payload.source,
        # NEW: latency (camera -> backend ingest)
        "lat_cam_to_backend": round(lat_cam_to_backend, 3),
    }

    _push_detect(base); _spawn(detect_broker.publish, base)

    # bed_empty / bed_occupied (internal only; no user DM)
    if clsname in {"bed_occupied","bedoccupied","bed_empty","bedempty"}:
        if clsname.startswith("bed_empty") or clsname == "bedempty":
            _mark_bed_empty(printer_id)
            _cancel_bed_watcher(printer_id)

            state = "unknown"
            try:
                jobinfo = await _backend_read_job(printer_id)
                mapped = (jobinfo.get("mapped") or {})
                state = (mapped.get("state") or "").strip().lower()
            except Exception:
                pass

            if state not in ("printing","paused"):
                _ = await _call_internal(
                    f"/internal/printers/{printer_id}/queue/process-next?force=1",
                    reason="bed_empty"
                )

            if BED_EMPTY_BROADCAST:
                await _emit_printer_event(printer_id, {
                    "event":"bed_empty","printer_id":printer_id,
                    "ts": datetime.utcnow().timestamp(),"auto_print_started": state not in ("printing","paused")
                })
            return {"ok": True, "bed_empty": True}
        return {"ok": True, "skipped": f"ignored bed status ({clsname})"}

    if event == "issue_update" and not ALERT_ON_UPDATE:
        await _emit_printer_event(printer_id, {
            "issue_active": event != "issue_cleared", "event": event, "printer_id": printer_id,
            "detected_class": payload.detected_class, "confidence": payload.confidence,
            "image_url": payload.image_url, "video_url": payload.video_url, "ts": payload.ts,
            "severity":"info","title":"Detector update (ignored by policy)","message": f"class={clsname} conf={conf:.2f}",
        })
        return {"ok": True, "skipped": "update events are disabled"}

    if not clsname:
        await _emit_printer_event(printer_id, {
            "issue_active": event != "issue_cleared", "event": event, "printer_id": printer_id,
            "detected_class": payload.detected_class, "confidence": payload.confidence,
            "image_url": payload.image_url, "video_url": payload.video_url, "ts": payload.ts,
            "severity":"info","title":"Detector event","message": f"class=? conf={conf:.2f}",
        })
        return {"ok": True, "skipped": "no detected_class"}

    if event != "issue_cleared":
        if clsname not in ALLOWED_DETECT_CLASSES:
            return {"ok": True, "skipped": f"class '{clsname}' not allowed"}
        if conf < MIN_DETECT_CONFIDENCE:
            return {"ok": True, "skipped": f"low confidence {conf:.2f} < {MIN_DETECT_CONFIDENCE:.2f}"}
    else:
        if clsname not in ALLOWED_DETECT_CLASSES:
            await _emit_printer_event(printer_id, {
                "issue_active": False, "event": event, "printer_id": printer_id,
                "detected_class": payload.detected_class, "confidence": payload.confidence,
                "image_url": payload.image_url, "video_url": payload.video_url, "ts": payload.ts,
                "severity":"info","title":"Cleared (ignored by policy)","message": f"class={clsname}",
            })
            return {"ok": True, "skipped": "cleared for non-allowed class"}

        # ==========================
    # block debounce + latency
    # ==========================
    if DETECT_CONFIRM_ENABLED and event in {"issue_started","issue_update"}:
        ts_now = _now_ts_fallback(payload.ts)
        st = _add_pending_hit(printer_id, clsname, ts=ts_now, conf=conf, payload=payload.model_dump())
        _gc_pending()
        confirmed, mean_conf = _check_confirm(st)

        # ===== LATENCY: debounce window (hit แรก -> ยืนยัน) =====
        first_ts = st.get("first_ts", ts_now)
        confirm_latency = max(0.0, ts_now - first_ts)
        log.info(
            "[LAT] debounce printer=%s cls=%s hits=%d mean_conf=%.3f "
            "window=%.1fs confirm_latency=%.3fs confirmed=%s",
            printer_id, clsname, st["hits"], mean_conf,
            DETECT_CONFIRM_WINDOW_SEC, confirm_latency, confirmed,
        )

        if not confirmed:
            await _emit_printer_event(printer_id, {
                "event":"detector_buffering","printer_id":printer_id,
                "detected_class": payload.detected_class, "confidence": payload.confidence,
                "hits": st["hits"], "mean_conf": round(mean_conf,3), "window_sec": DETECT_CONFIRM_WINDOW_SEC,
                "ts": ts_now, "severity":"info",
                "title":"Verifying anomaly...","message": f"class={clsname} hits={st['hits']} mean_conf={mean_conf:.2f}",
            })
            return {"ok": True, "deferred": True, "hits": st["hits"], "mean_conf": mean_conf}
        else:
            _clear_pending(printer_id, clsname)

    # Confirmed anomaly or cleared
    sev = payload.severity or _auto_severity(event, conf)
    title = {
        "issue_started":"Anomaly detected",
        "issue_update":"Anomaly update",
        "issue_cleared":"Back to normal",
    }[event]

    # ===== LATENCY: camera -> emit issue_* event =====
    now_ts2 = datetime.utcnow().timestamp()
    lat_cam_to_emit = max(0.0, now_ts2 - src_ts) if src_ts > 0 else 0.0

    log.info(
        "[LAT] emit_issue printer=%s event=%s cls=%s lat_cam_to_backend=%.3fs lat_cam_to_emit=%.3fs",
        printer_id, event, clsname, lat_cam_to_backend, lat_cam_to_emit,
    )

    # เติมข้อมูลงานที่อยู่บนเตียงให้การ์ด/DM
    job_on_bed = _find_active_job(db, printer_id)
    job_id   = job_on_bed.id if job_on_bed else None
    job_name = (job_on_bed.name or "").strip() if job_on_bed else None

    # ---- build unified reason text & data ----
    reason_txt = _reason_label(clsname)
    cls_part = f"{reason_txt} ({conf:.2f})" if payload.detected_class else "-"
    base_name = job_name or (payload.status_text or reason_txt) or "-"
    msg = (
        f"{base_name}  • source={payload.source or 'detect_stream'}  "
        f"printer={printer_id}  reason={cls_part}  "
        f"boxes={len(payload.boxes or [])}  (Bangkok time { _fmt_bkk() })"
    )

    data = payload.model_dump()
    data["printer_id"] = printer_id
    if job_id is not None:
        data["job_id"] = job_id
    if job_name:
        data["name"] = job_name
        data["job_name"] = job_name
    data["status"] = _canon_status(data.get("status"))
    # NEW: explicit reason fields
    data["reason"] = clsname
    data["reason_label"] = reason_txt
    # NEW: latency fields
    data["lat_cam_to_backend"] = round(lat_cam_to_backend, 3)
    data["lat_cam_to_emit"] = round(lat_cam_to_emit, 3)

    # auto-pause
    if AUTO_PAUSE:
        allow_event = event in PAUSE_ON_EVENTS
        allow_class = clsname in PAUSE_ON_CLASSES
        conf_ok = conf >= PAUSE_MIN_CONF
        log.info("[PAUSE] check event=%s class=%s conf=%.2f | allow_event=%s allow_class=%s conf_ok=%s",
                 event, clsname, conf, allow_event, allow_class, conf_ok)
        if allow_event and allow_class and conf_ok:
            await _pause_octoprint()
            job = _pause_current_job_in_db(db, printer_id)
            if job:
                primary_emp = _primary_emp_from_job(job)
                # Holo/web panel + toast
                await _emit_printer_event(printer_id, {
                    "event":"job_paused","printer_id":printer_id,"job_id":job.id,"name":job.name,
                    "ts": datetime.utcnow().timestamp(),"severity":"warning",
                    "title":"⏸️ Print paused","message": f"{job.name} • {reason_txt} ({conf:.2f})",
                    "owner_employee_id": (job.employee_id or "").strip(),
                    "requested_by_employee_id": (getattr(job, "requested_by_employee_id", "") or "").strip(),
                })
                persist_key = f"pause-{printer_id}"
                panel_payload = {
                    "event":"alert","printer_id":printer_id,"ts": datetime.utcnow().timestamp(),
                    "severity":"warning","title":"⏸️ Print paused (anomaly detected)",
                    "message": f"{job.name} • {reason_txt} ({conf:.2f})",
                    "ui": {"type":"panel","variant":"warning","sticky":True,"timeout_ms":0,"require_action":True,
                           "persist_key":persist_key,"actions":[
                               {"type":"button","label":"Resume","command":"pause","action":"resume"},
                               {"type":"button","label":"Cancel","command":"cancel"}]},
                    "owner_employee_id": (job.employee_id or "").strip(),
                    "requested_by_employee_id": (getattr(job, "requested_by_employee_id", "") or "").strip(),
                }
                _spawn(_panel_watchdog, printer_id, panel_payload, interval=12.0)

                # แจ้งเจ้าของจริง: BOTH paused + issue (same reason)
                ev_paused = format_canonical_event(
                    type="print.paused", status="paused", severity="warning",
                    title="⏸️ Print paused (anomaly detected)",
                    message=f"{job.name} • {reason_txt} ({conf:.2f})",
                    printer_id=printer_id,
                    data={"job_id": job.id, "name": job.name, "detected_class": clsname, "confidence": conf,
                          "reason": clsname, "reason_label": reason_txt}
                )
                if primary_emp:
                    await emit_canonical_event(db, primary_emp, ev_paused)

                ev_issue_owner = format_canonical_event(
                    type="print.issue", status="issue", severity="critical",
                    title="🛑 Anomaly detected",
                    message=f"{job.name}  • printer={printer_id}  reason={reason_txt} ({conf:.2f})  (Bangkok time { _fmt_bkk() })",
                    printer_id=printer_id,
                    data={"job_id": job.id, "name": job.name, "detected_class": clsname, "confidence": conf,
                          "reason": clsname, "reason_label": reason_txt}
                )
                if primary_emp:
                    await emit_canonical_event(db, primary_emp, ev_issue_owner)

    # broadcast anomaly / cleared (ใส่ชื่อ/ไอดีงานด้วย)
    current_owner = _find_active_owner(db, printer_id)
    await _emit_printer_event(printer_id, {
        "issue_active": event != "issue_cleared", "event": event, "printer_id": printer_id,
        "detected_class": payload.detected_class, "confidence": payload.confidence,
        "image_url": payload.image_url, "video_url": payload.video_url, "boxes": payload.boxes,
        "image_w": payload.image_w, "image_h": payload.image_h, "ts": payload.ts,
        "severity": sev, "title": title, "message": msg,
        "owner_employee_id": (current_owner or ""),
        "name": job_name or None,
        "job_id": job_id if job_id is not None else None,
        "reason": clsname, "reason_label": reason_txt,
        # NEW: latency info to UI
        "lat_cam_to_backend": round(lat_cam_to_backend, 3),
        "lat_cam_to_emit": round(lat_cam_to_emit, 3),
    })

    # notify recipients -> ใช้ canonical event เดียวกัน
    recipients = _normalize_recipients(payload.recipients)
    if not recipients:
        owner = _find_active_owner(db, printer_id)
        if owner: recipients = [owner]
    if not recipients and DEFAULT_ALERT_RECIPIENTS:
        recipients = list(DEFAULT_ALERT_RECIPIENTS)

    ev_issue = format_canonical_event(
        type="print.issue", status="issue", severity=sev, title=title, message=msg,
        printer_id=printer_id, data=data
    )
    for emp in recipients:
        # ตอนนี้เจ้าของก็ได้รับ issue แล้ว (ด้านบน) แต่ถ้าหลุดมาก็ยังยิงซ้ำได้ไม่เป็นไร
        await emit_canonical_event(db, emp, ev_issue)

    if event == "issue_cleared":
        try: await _close_sticky_panel(printer_id, persist_key=f"pause-{printer_id}")
        except Exception: pass

    return {"ok": True, "recipients": recipients, "severity": sev, "class": clsname, "confidence": conf}

# =============================================================================
# Job lifecycle hook (Completed / Cancelled / Failed)
# =============================================================================
@router.post("/job-event")
async def job_event(
    payload: JobEventIn,
    db: Session = Depends(get_db),
    x_admin_token: str = Header(default="")
):
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    job: Optional[PrintJob] = db.query(PrintJob).filter(PrintJob.id == payload.job_id).first()
    if not job: raise HTTPException(status_code=404, detail="job_not_found")

    # คนที่ควรถูกแจ้งเตือน (requested_by → fallback employee_id)
    primary_emp = _primary_emp_from_job(job)
    has_primary = bool(primary_emp)
    file_owner = (job.employee_id or "").strip()  # เก็บไว้เผื่อใช้ debug / data ต่อ

    printer_id = (payload.printer_id or job.printer_id or DEFAULT_PRINTER_ID or "-").strip().lower()
    title, base_sev = _status_title_severity(payload.status)

    cls_raw = payload.detected_class or ""
    cls_norm = _norm_cls(cls_raw)
    reason_txt = _reason_label(cls_norm)
    conf = float(payload.confidence or 0.0)

    if _announced(printer_id, job.id, payload.status): return {"ok": True, "skipped":"already_announced"}
    if _should_skip_job_event(printer_id, job.id, payload.status): return {"ok": True, "skipped":"duplicate_recent"}

    if payload.status in ("cancelled","failed"): _suppress_after_cancel(printer_id)

    extra = f" • reason: {reason_txt} ({conf:.2f})" if payload.status == "failed" and cls_norm in ALLOWED_FAIL_DETECT_CLASSES else ""
    msg = (
        f"printer={printer_id} • job_id={job.id} • {job.name or payload.name or '-'}"
        f"{extra} • {payload.status}  (Bangkok time { _fmt_bkk(payload.finished_at or job.finished_at) })"
    )

    data = {"job_id":job.id,"printer_id":printer_id,"status":payload.status,"name":job.name or payload.name,
            "detected_class": cls_raw or None, "confidence": payload.confidence,
            "reason": cls_norm or None, "reason_label": reason_txt if cls_norm else None,
            "finished_at": (payload.finished_at or job.finished_at or datetime.utcnow()).isoformat()}

    evt = {"completed":"job_completed","cancelled":"job_cancelled","failed":"job_failed"}.get(payload.status,"job_status")
    await _emit_printer_event(printer_id, {
        "event": evt, "printer_id": printer_id, "job_id": job.id, "status": payload.status,
        "title": title, "message": msg, "severity": base_sev, "detected_class": cls_raw or None,
        "confidence": payload.confidence, "name": job.name or payload.name, "ts": datetime.utcnow().timestamp(),
        "owner_employee_id": (job.employee_id or "").strip(),
        "requested_by_employee_id": (getattr(job, "requested_by_employee_id", "") or "").strip(),
        "reason": cls_norm or None, "reason_label": reason_txt if cls_norm else None,
    })

    try: await _close_sticky_panel(printer_id, persist_key=f"pause-{printer_id}")
    except Exception: pass

    if has_primary:
        ev_owner = format_canonical_event(
            type=f"print.{ 'canceled' if payload.status=='cancelled' else payload.status }",
            status=('canceled' if payload.status=='cancelled' else payload.status),
            severity=base_sev, title=title, message=msg,
            printer_id=printer_id, data=data
        )
        await emit_canonical_event(db, primary_emp, ev_owner)

    if payload.status == "completed":
        finished_at = payload.finished_at or job.finished_at or datetime.utcnow()
        _cancel_bed_watcher(printer_id)
        _BED_WATCHERS[printer_id] = asyncio.create_task(
            _start_bed_timeout_watcher(printer_id, finished_at, primary_emp if has_primary else None)
        )

    _mark_announced(printer_id, job.id, payload.status)
    return {"ok": True, "owner": primary_emp if has_primary else None, "status": payload.status}

# =============================================================================
# RECENT DETECTS: REST + SSE + simple view (debug/monitor)
# =============================================================================
def _flt_detects(*, printer_id: str | None, detected_class: str | None, event: str | None, since_ts: float | None) -> list[DetectRecord]:
    out: list[DetectRecord] = []
    for rec in reversed(RECENT_DETECTS):
        if printer_id and (rec.printer_id or "") != printer_id: continue
        if detected_class and (rec.detected_class or "") != detected_class: continue
        if event and (rec.event or "") != event: continue
        if since_ts is not None and float(rec.ts or 0) < float(since_ts): continue
        out.append(rec)
    return out

@router.get("/detects")
def list_detects(
    limit: int = Query(default=50, ge=1, le=500),
    printer_id: str | None = Query(default=None),
    detected_class: str | None = Query(default=None),
    event: str | None = Query(default=None),
    since_ts: float | None = Query(default=None),
    x_admin_token: str = Header(default=""),
    token: str | None = Query(default=None),
):
    admin_ok = (ADMIN_TOKEN and ((x_admin_token == ADMIN_TOKEN) or (token == ADMIN_TOKEN)))
    if not admin_ok:
        raise HTTPException(status_code=401, detail="Unauthorized")
    pid = (printer_id or "").strip().lower() or None
    cls = (detected_class or "").strip().lower() or None
    evt = (event or "").strip() or None
    recs = _flt_detects(printer_id=pid, detected_class=cls, event=evt, since_ts=since_ts)
    return [r.model_dump() for r in recs[:limit]]

@router.get("/detects/stream")
async def stream_detects(
    request: Request,
    x_admin_token: str = Header(default=""),
    token: str | None = Query(default=None),
):
    admin_ok = (ADMIN_TOKEN and ((x_admin_token == ADMIN_TOKEN) or (token == ADMIN_TOKEN)))
    if not admin_ok:
        raise HTTPException(status_code=401, detail="Unauthorized")
    q = await detect_broker.subscribe()
    async def gen():
        yield ":ok\n\n"
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    if await request.is_disconnected(): break
                    yield ":keepalive\n\n"
        finally:
            detect_broker.unsubscribe(q)
    headers = {
        "Cache-Control":"no-cache", "X-Accel-Buffering":"no", "Connection":"keep-alive",
        "Access-Control-Allow-Origin":"*",
    }
    return StreamingResponse(gen(), media_type="text/event-stream; charset=utf-8", headers=headers)

@router.get("/detects/view")
def view_detects_page(x_admin_token: str = Header(default=""), token: str | None = Query(default=None)):
    admin_ok = (ADMIN_TOKEN and ((x_admin_token == ADMIN_TOKEN) or (token == ADMIN_TOKEN)))
    if not admin_ok:
        raise HTTPException(status_code=401, detail="Unauthorized")
    token_q = f"?token={ADMIN_TOKEN}" if ADMIN_TOKEN else ""
    html = f"""
<!doctype html>
<meta charset="utf-8">
<title>Detects Live</title>
<style>
body {{ font-family: ui-sans-serif, system-ui, -apple-system; line-height:1.4; margin: 16px; }}
pre {{ background:#0b1020; color:#d6ffe8; padding:12px; border-radius:8px; max-height:70vh; overflow:auto; }}
small {{ color:#7a869a; }}
code {{ color:#97e6ff; }}
</style>
<h2>Detects (Live)</h2>
<p><small>Streaming from <code>/notifications/detects/stream</code></small></p>
<pre id="log"></pre>
<script>
const log = document.getElementById('log');
const es = new EventSource('/notifications/detects/stream{token_q}');
es.onmessage = (e) => {{
  try {{
    const obj = JSON.parse(e.data);
    const t = obj.ts ? new Date(obj.ts*1000).toLocaleTimeString() : new Date().toLocaleTimeString();
    const cls = obj.reason_label || obj.detected_class || '-';
    const conf = obj.confidence != null ? Number(obj.confidence).toFixed(2) : '-';
    const line = `[${{t}}] ${{obj.event}}  ${{obj.printer_id || '-'}}  ${{cls}} (${{conf}})`;
    log.textContent = line + "\\n" + log.textContent;
  }} catch (err) {{
    log.textContent = e.data + "\\n" + log.textContent;
  }}
}};
es.onerror = () => {{ log.textContent = "[error] stream disconnected\\n" + log.textContent; }};
</script>
"""
    return HTMLResponse(content=html)

# =============================================================================
# Optional: REST to close HoloLens panel
# =============================================================================
@router.post("/alert/close")
async def close_panel(
    printer_id: str = Query(...),
    persist_key: str = Query(...),
    x_admin_token: str = Header(default=""),
    token: str | None = Query(default=None),
):
    admin_ok = (ADMIN_TOKEN and ((x_admin_token == ADMIN_TOKEN) or (token == ADMIN_TOKEN)))
    if not admin_ok:
        raise HTTPException(status_code=401, detail="Unauthorized")
    await _close_sticky_panel(printer_id, persist_key=persist_key)
    return {"ok": True}

@router.get("/bed/status")
def bed_status(printer_id: str, x_admin_token: str = Header(default="")):
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    pid = (printer_id or "").strip().lower()
    ts = _BED_EMPTY_TS.get(pid)
    if not ts:
        return {"ok": False, "reason": "no_bed_empty_seen"}
    age = (datetime.utcnow() - ts).total_seconds()
    return {"ok": True, "last_empty_ts": ts.timestamp(), "age_sec": age}

# =============================================================================
# LatencyLog helper (เก็บลง DB)
# =============================================================================
def _log_latency_row(
    db: Session,
    *,
    channel: str,
    path: str,
    t_send_epoch: float,
    note: str | None = None,
) -> None:
    """
    บันทึก latency ง่าย ๆ ลง latency_logs
    - ใช้ t_send_epoch (float epoch seconds) → แปลงเป็น DateTime(tz=UTC)
    - t_recv = datetime.now(timezone.utc)
    """
    try:
        if not t_send_epoch or t_send_epoch <= 0:
            return

        t_send = datetime.fromtimestamp(float(t_send_epoch), tz=timezone.utc)
        t_recv = datetime.now(timezone.utc)
        latency_ms = max(0.0, (t_recv - t_send).total_seconds() * 1000.0)

        row = LatencyLog(
            channel=(channel or "backend")[:32],
            path=(path or "")[:255],
            t_send=t_send,
            t_recv=t_recv,
            latency_ms=latency_ms,
            note=(note or None)[:255] if note else None,
        )
        db.add(row)
        db.commit()
    except Exception:
        log.exception("[LAT] failed to insert LatencyLog")
        try:
            db.rollback()
        except Exception:
            pass
