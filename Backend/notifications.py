# backend/notifications.py
from __future__ import annotations

import os, re, asyncio, json, logging, inspect
from datetime import datetime, timedelta
from typing import Dict, Set, Literal, Optional, List, Tuple, Union
from collections import deque

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Query, Header, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse, HTMLResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel

from db import get_db, SessionLocal
from models import Notification, NotificationTarget, User, PrintJob
from schemas import NotificationOut, NotificationCreate, NotificationMarkRead
from auth import get_current_user, decode_token
from emailer import send_notification_email
from teams_webhook import send_teams_notification

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
# Fire & forget (รองรับทั้ง async/sync)
# =============================================================================
def _spawn(func_or_coro, /, *args, **kwargs) -> None:
    try:
        if inspect.iscoroutine(func_or_coro):
            asyncio.create_task(func_or_coro)  # already a coroutine object
        elif inspect.iscoroutinefunction(func_or_coro):
            asyncio.create_task(func_or_coro(*args, **kwargs))
        else:
            asyncio.create_task(asyncio.to_thread(func_or_coro, *args, **kwargs))
    except Exception:
        log.exception("[BG] spawn failed")

# =============================================================================
# ENV / CONFIG
# =============================================================================
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
DEFAULT_PRINTER_ID = os.getenv("DEFAULT_PRINTER_ID", "").strip()
BACKEND_INTERNAL_BASE = os.getenv("BACKEND_INTERNAL_BASE", "http://127.0.0.1:8001").rstrip("/")

async def _call_internal(path: str) -> dict:
    url = f"{BACKEND_INTERNAL_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, headers={"X-Admin-Token": ADMIN_TOKEN})
            r.raise_for_status()
            return r.json()
    except Exception:
        return {"ok": False, "error": "internal_call_failed"}

# Policy – detect classes
ALLOWED_DETECT_CLASSES = {
    s.strip().lower()
    for s in os.getenv("ALLOWED_DETECT_CLASSES", "cracks,layer_shift,spaghetti,stringing").split(",")
    if s.strip()
}
MIN_DETECT_CONFIDENCE = float(os.getenv("MIN_DETECT_CONFIDENCE", "0.70"))
ALERT_ON_UPDATE = os.getenv("ALERT_ON_UPDATE", "1").strip().lower() not in ("0", "false")

BED_EMPTY_BROADCAST = os.getenv("BED_EMPTY_BROADCAST", "0").strip().lower() not in {"0","false","no","off"}

# Auto-pause policy
AUTO_PAUSE = os.getenv("AUTO_PAUSE_ON_DETECT", "1").strip().lower() not in ("0", "false")
PAUSE_ON_EVENTS = {s.strip().lower() for s in os.getenv("PAUSE_ON_EVENTS", "issue_started,issue_update").split(",") if s.strip()}
PAUSE_ON_CLASSES = {s.strip().lower() for s in os.getenv("PAUSE_ON_CLASSES", "cracks,layer_shift,spaghetti,stringing").split(",") if s.strip()}
PAUSE_MIN_CONF = float(os.getenv("PAUSE_MIN_CONFIDENCE", "0.70"))

# --- Confirm window for detector (debounce before alert) ---
DETECT_CONFIRM_ENABLED = os.getenv("DETECT_CONFIRM_ENABLED", "1").strip().lower() not in {"0","false","no","off"}
DETECT_CONFIRM_WINDOW_SEC = float(os.getenv("DETECT_CONFIRM_WINDOW_SEC", "5"))
DETECT_CONFIRM_MIN_HITS   = int(os.getenv("DETECT_CONFIRM_MIN_HITS", "2"))
DETECT_CONFIRM_MIN_MEAN_CONF = float(os.getenv("DETECT_CONFIRM_MIN_MEAN_CONF", "0.78"))

# OctoPrint
def _clean_env(v: Optional[str]) -> str:
    return (v or "").strip().strip('"').strip("'")

OCTO_BASE = _clean_env(os.getenv("OCTOPRINT_BASE") or "").rstrip("/")
OCTO_KEY  = _clean_env(os.getenv("OCTOPRINT_API_KEY") or "")
try:
    OCTO_TIMEOUT = float(_clean_env(os.getenv("OCTOPRINT_HTTP_TIMEOUT") or os.getenv("OCTOPRINT_TIMEOUT") or "10"))
except Exception:
    OCTO_TIMEOUT = 10.0

def _octo_ready() -> bool:
    return bool(OCTO_BASE and OCTO_KEY)

def _octo_headers() -> dict:
    return {"X-Api-Key": OCTO_KEY, "Accept": "application/json"}

router = APIRouter(prefix="/notifications", tags=["notifications"])

# =============================================================================
# DEDUPE (ช่วงสั้น) + ANNOUNCE-ONCE (ช่วงยาว) + SUPPRESS หลัง cancel/failed
# =============================================================================
JOB_EVENT_DEDUP_TTL_SEC = int(os.getenv("JOB_EVENT_DEDUP_TTL_SEC", "15"))
_recent_job_events: Dict[str, datetime] = {}

def _dupkey(printer_id: str, job_id: Union[int, str, None], status: str) -> str:
    return f"{(printer_id or '').lower()}|{job_id or '-'}|{(status or '').lower()}"

def _should_skip_job_event(printer_id: str, job_id: Union[int, str, None], status: str) -> bool:
    now = datetime.utcnow()
    k = _dupkey(printer_id, job_id, status)
    ts = _recent_job_events.get(k)
    if ts and (now - ts).total_seconds() < JOB_EVENT_DEDUP_TTL_SEC:
        return True
    _recent_job_events[k] = now
    if len(_recent_job_events) > 500:
        cutoff = now - timedelta(seconds=JOB_EVENT_DEDUP_TTL_SEC * 2)
        for kk, vv in list(_recent_job_events.items()):
            if vv < cutoff:
                _recent_job_events.pop(kk, None)
    return False

ANNOUNCE_TTL_HOURS = int(os.getenv("ANNOUNCE_TTL_HOURS", "12"))
ANNOUNCED_JOB_STATUS: Dict[Tuple[str, int, str], datetime] = {}

def _announced(printer_id: str, job_id: Optional[int], status: str) -> bool:
    if not job_id:
        return False
    k = ((printer_id or "").lower(), int(job_id), (status or "").lower())
    ts = ANNOUNCED_JOB_STATUS.get(k)
    if not ts:
        return False
    if (datetime.utcnow() - ts).total_seconds() > ANNOUNCE_TTL_HOURS * 3600:
        ANNOUNCED_JOB_STATUS.pop(k, None)
        return False
    return True

def _mark_announced(printer_id: str, job_id: Optional[int], status: str):
    if not job_id:
        return
    k = ((printer_id or "").lower(), int(job_id), (status or "").lower())
    ANNOUNCED_JOB_STATUS[k] = datetime.utcnow()

# SUPPRESS: กัน bed_empty → completed หลอน หลัง cancel/failed
SUPPRESS_AFTER_CANCEL_SEC = int(os.getenv("SUPPRESS_AFTER_CANCEL_SEC", "25"))
_SUPPRESS_UNTIL: Dict[str, datetime] = {}  # key = printer_id(lower) -> until(UTC)

def _suppress_after_cancel(printer_id: str, seconds: Optional[int] = None) -> None:
    sec = int(seconds or SUPPRESS_AFTER_CANCEL_SEC)
    pid = (printer_id or "").strip().lower()
    if not pid or sec <= 0:
        return
    _SUPPRESS_UNTIL[pid] = datetime.utcnow() + timedelta(seconds=sec)

def _is_suppressed(printer_id: str) -> Optional[str]:
    pid = (printer_id or "").strip().lower()
    until = _SUPPRESS_UNTIL.get(pid)
    if not until:
        return None
    now = datetime.utcnow()
    if now >= until:
        _SUPPRESS_UNTIL.pop(pid, None)
        return None
    return until.isoformat()

# =============================================================================
# Bed-empty normalization / mapping
# =============================================================================
def _norm_cls(s: Optional[str]) -> str:
    return (s or "").strip().lower().replace("-", "_").replace("  ", " ").replace(" ", "_")

BED_EMPTY_ALIASES: Set[str] = {
    s.strip().lower().replace("-", "_").replace(" ", "_")
    for s in os.getenv("BED_EMPTY_ALIASES", "bed empty,bed_empty,bed-empty,bedempty").split(",")
    if s.strip()
}

# ----- Silent classes (ไม่ alert / ไม่ broadcast) -----
def _norm(s: str) -> str:
    return (s or "").strip().lower().replace("-", "_").replace("  ", " ").replace(" ", "_")

SILENT_DETECT_CLASSES = {
    _norm(s) for s in os.getenv(
        "SILENT_DETECT_CLASSES",
        "bed empty,bed_empty,bed-empty,bed occupied,bed_occupied,bed-occupied"
    ).split(",") if s.strip()
}

def _is_silent_class(raw: Optional[str]) -> bool:
    return _norm(raw or "") in SILENT_DETECT_CLASSES

def _parse_ids(env: str) -> Set[int]:
    out: Set[int] = set()
    for tok in env.split(","):
        tok = tok.strip()
        if not tok: continue
        try:
            out.add(int(tok))
        except Exception:
            pass
    return out

BED_EMPTY_CLASS_IDS: Set[int] = _parse_ids(os.getenv("BED_EMPTY_CLASS_IDS", ""))

def _is_bed_empty(detected_class: Optional[str]) -> bool:
    cls = _norm_cls(detected_class)
    if not cls:
        return False
    if cls in BED_EMPTY_ALIASES:
        return True
    m = re.match(r"^class_(\d+)$", cls)
    if m:
        try:
            return int(m.group(1)) in BED_EMPTY_CLASS_IDS
        except Exception:
            return False
    return False

BED_EMPTY_REQUIRE_RECENT_COMPLETION = os.getenv("BED_EMPTY_REQUIRE_RECENT_COMPLETION", "1").strip().lower() not in ("0", "false")
BED_EMPTY_COMPLETION_WINDOW_SEC = int(os.getenv("BED_EMPTY_COMPLETION_WINDOW_SEC", "600") or "600")

# =============================================================================
# Extra helpers: default recipients & active owner
# =============================================================================
def _parse_employees_csv(s: str | None) -> list[str]:
    out: list[str] = []
    for tok in (s or "").split(","):
        tok = tok.strip()
        if tok:
            out.append(tok)
    return out

DEFAULT_ALERT_RECIPIENTS: list[str] = _parse_employees_csv(os.getenv("ALERT_DEFAULT_EMPLOYEES", ""))

def _find_active_owner(db: Session, printer_id: str) -> Optional[str]:
    pid = (printer_id or DEFAULT_PRINTER_ID or "-").strip().lower()
    job = (
        db.query(PrintJob)
          .filter(PrintJob.printer_id == pid, PrintJob.status.in_(("processing","printing","paused")))
          .order_by(PrintJob.started_at.desc().nullslast(), PrintJob.id.desc())
          .first()
    )
    emp = (job.employee_id or "").strip() if job else ""
    return emp or None

# =============================================================================
# WS for Unity
# =============================================================================
class UnityAlertHub:
    def __init__(self):
        self.rooms: dict[str, set[WebSocket]] = {}

    async def connect(self, ws: WebSocket, printer_id: str):
        await ws.accept()
        self.rooms.setdefault(printer_id, set()).add(ws)

    def disconnect(self, ws: WebSocket, printer_id: str):
        room = self.rooms.get(printer_id)
        if not room:
            return
        room.discard(ws)
        if not room:
            self.rooms.pop(printer_id, None)
        star = self.rooms.get("*", set())
        if ws in star:
            star.discard(ws)
            if not star:
                self.rooms.pop("*", None)

    async def broadcast(self, printer_id: str, payload: dict):
        targets = set()
        if printer_id in self.rooms:
            targets |= self.rooms[printer_id]
        if "*" in self.rooms:
            targets |= self.rooms["*"]

        dead = []
        data = json.dumps(payload, ensure_ascii=False)
        for ws in list(targets):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)

        if dead:
            for key, room in list(self.rooms.items()):
                for ws in dead:
                    if ws in room:
                        room.discard(ws)
                if not room:
                    self.rooms.pop(key, None)

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
# SSE broker (per-user notifications)
# =============================================================================
class NotificationBroker:
    def __init__(self):
        self.subscribers: Dict[str, Set[asyncio.Queue]] = {}

    async def subscribe(self, emp: str) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=200)
        self.subscribers.setdefault(emp, set()).add(q)
        return q

    def unsubscribe(self, emp: str, q: asyncio.Queue):
        qs = self.subscribers.get(emp)
        if not qs:
            return
        qs.discard(q)
        if not qs:
            self.subscribers.pop(emp, None)

    async def publish(self, emp: str, payload: dict):
        for q in list(self.subscribers.get(emp, set())):
            try:
                await q.put(payload)
            except Exception:
                self.unsubscribe(emp, q)

broker = NotificationBroker()

# =============================================================================
# Detects ring buffer + SSE (system-level)
# =============================================================================
RECENT_DETECT_MAX = int(os.getenv("RECENT_DETECT_MAX", "500"))
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
        RECENT_DETECTS.append(
            DetectRecord(
                ts = float(payload.get("ts") or 0),
                event = str(payload.get("event") or ""),
                printer_id = payload.get("printer_id"),
                detected_class = payload.get("detected_class"),
                confidence = payload.get("confidence"),
                image_url = payload.get("image_url"),
                video_url = payload.get("video_url"),
                boxes = payload.get("boxes"),
                image_w = payload.get("image_w"),
                image_h = payload.get("image_h"),
                source = payload.get("source"),
            )
        )
    except Exception:
        pass

class DetectSSEBroker:
    def __init__(self):
        self.subs: set[asyncio.Queue] = set()

    async def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=500)
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self.subs.discard(q)

    async def publish(self, payload: dict):
        dead: list[asyncio.Queue] = []
        for q in list(self.subs):
            try:
                await q.put(payload)
            except Exception:
                dead.append(q)
        for q in dead:
            self.unsubscribe(q)

detect_broker = DetectSSEBroker()

# ---- Pending issue confirmation state (debounce/confirm) ----
_PENDING_ISSUES: dict[tuple[str,str], dict] = {}

def _pend_key(printer_id: str, clsname: str) -> tuple[str,str]:
    return ((printer_id or "-").strip().lower(), (clsname or "").strip().lower())

def _now_ts_fallback(ts: Optional[float]) -> float:
    try:
        t = float(ts or 0.0)
        return t if t > 0 else datetime.utcnow().timestamp()
    except Exception:
        return datetime.utcnow().timestamp()

def _add_pending_hit(printer_id: str, clsname: str, *, ts: float, conf: float, payload: dict):
    k = _pend_key(printer_id, clsname)
    st = _PENDING_ISSUES.get(k) or {
        "first_ts": ts, "last_ts": ts, "hits": 0,
        "sum_conf": 0.0, "max_conf": 0.0, "last_payload": None
    }
    st["hits"] += 1
    st["last_ts"] = ts
    st["sum_conf"] += float(conf or 0.0)
    st["max_conf"] = max(st["max_conf"], float(conf or 0.0))
    st["last_payload"] = payload
    _PENDING_ISSUES[k] = st
    return st

def _clear_pending(printer_id: str, clsname: str):
    _PENDING_ISSUES.pop(_pend_key(printer_id, clsname), None)

def _check_confirm(st: dict) -> tuple[bool, float]:
    dt = st["last_ts"] - st["first_ts"]
    hits = st["hits"]
    mean_conf = (st["sum_conf"] / hits) if hits > 0 else 0.0
    ok_window = dt >= DETECT_CONFIRM_WINDOW_SEC
    ok_hits   = hits >= DETECT_CONFIRM_MIN_HITS
    ok_conf   = mean_conf >= DETECT_CONFIRM_MIN_MEAN_CONF
    return (ok_window and ok_hits and ok_conf), mean_conf

def _gc_pending():
    cutoff = datetime.utcnow().timestamp() - (2.0 * max(1.0, DETECT_CONFIRM_WINDOW_SEC))
    for k, st in list(_PENDING_ISSUES.items()):
        if st.get("last_ts", 0) < cutoff:
            _PENDING_ISSUES.pop(k, None)

# =============================================================================
# Sticky alert watchdog (keep panel alive until cleared)
# =============================================================================
_PAUSE_PANEL_ON: Dict[str, bool] = {}   # key = printer_id(lower)

async def _panel_watchdog(printer_id: str, payload: dict, interval: float = 12.0):
    pid = (printer_id or "-").strip().lower()
    _PAUSE_PANEL_ON[pid] = True
    try:
        # ping รอบแรก
        await unity_ws.broadcast(pid, payload)
        # จากนั้น re-ping เรื่อย ๆ จนถูกสั่งหยุด
        while _PAUSE_PANEL_ON.get(pid, False):
            await asyncio.sleep(interval)
            await unity_ws.broadcast(pid, payload)
    except Exception:
        logging.exception("[PANEL] watchdog error")
    finally:
        _PAUSE_PANEL_ON[pid] = False

def _stop_panel_watchdog(printer_id: str):
    pid = (printer_id or "-").strip().lower()
    _PAUSE_PANEL_ON[pid] = False

async def _close_sticky_panel(printer_id: str, persist_key: str):
    """สั่งปิดพาเนลฝั่ง Unity และหยุด watchdog"""
    _stop_panel_watchdog(printer_id)
    try:
        await unity_ws.broadcast(printer_id, {
            "event": "alert_close",
            "printer_id": printer_id,
            "persist_key": persist_key,
            "ts": datetime.utcnow().timestamp(),
        })
    except Exception:
        logging.exception("[PANEL] close panel error")

# =============================================================================
# Helpers (notify / pause / job read)
# =============================================================================
def _send_email_bg(emp_id: str, ntype: str, title: str, message: str | None, data: dict | None):
    db2 = SessionLocal()
    try:
        send_notification_email(
            db2, emp_id,
            ntype=ntype, title=title or "", message=message,
            data=data or None,
        )
    except Exception:
        logging.exception("[NOTIFY] email bg failed")
    finally:
        try:
            db2.close()
        except Exception:
            pass

def _send_teams_bg(ntype: str, title: str, message: str | None, data: dict | None):
    try:
        send_teams_notification(
            ntype=ntype, title=title or "", message=message,
            data=data or None,
        )
    except Exception:
        logging.exception("[NOTIFY] teams bg failed")

async def notify_user(
    db: Session,
    employee_id: str,
    *,
    type: str,
    title: str,
    message: str | None = None,
    severity: str = "info",
    data: dict | None = None,
) -> Notification:
    n = Notification(
        ntype=type, title=title, message=message,
        severity=severity, data_json=json.dumps(data or {})
    )
    db.add(n); db.commit(); db.refresh(n)

    # target
    db.add(NotificationTarget(notification_id=n.id, employee_id=employee_id))
    db.commit()

    # push SSE ให้ client
    event = {
        "id": n.id,
        "type": n.ntype,
        "severity": n.severity,
        "title": n.title,
        "message": n.message,
        "data": json.loads(n.data_json) if n.data_json else None,
        "created_at": n.created_at.isoformat(),
        "read": False,
    }
    await broker.publish(employee_id, event)

    # background: ไม่แชร์ DB session
    try:
        asyncio.create_task(asyncio.to_thread(
            _send_email_bg,
            employee_id, n.ntype, n.title or "", n.message, json.loads(n.data_json) if n.data_json else None
        ))
    except Exception:
        logging.exception("[NOTIFY] schedule email failed")

    try:
        asyncio.create_task(asyncio.to_thread(
            _send_teams_bg,
            n.ntype, n.title or "", n.message, json.loads(n.data_json) if n.data_json else None
        ))
    except Exception:
        logging.exception("[NOTIFY] schedule teams failed")

    return n

async def notify_many(db: Session, employee_ids: list[str], **kwargs):
    for emp in employee_ids:
        await notify_user(db, emp, **kwargs)

async def _pause_octoprint() -> None:
    if not _octo_ready():
        log.warning("[PAUSE] OctoPrint not configured; skip")
        return
    url = f"{OCTO_BASE}/api/job"
    payload = {"command": "pause", "action": "pause"}
    timeout = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                url,
                headers={**_octo_headers(), "Content-Type": "application/json"},
                json=payload,
            )
            log.info("[PAUSE] POST %s -> %s %s", url, r.status_code, r.text[:200])
            r.raise_for_status()
    except httpx.HTTPError as e:
        sc = getattr(e.response, "status_code", None)
        log.exception("[PAUSE] HTTPError %s", sc)
    except Exception:
        log.exception("[PAUSE] unexpected error")

def _pause_current_job_in_db(db: Session, printer_id: str) -> Optional[PrintJob]:
    pid = (printer_id or DEFAULT_PRINTER_ID or "-").strip().lower()
    job = (
        db.query(PrintJob)
          .filter(
              PrintJob.printer_id == pid,
              PrintJob.status.in_(("processing", "printing"))
          )
          .order_by(PrintJob.started_at.desc(), PrintJob.id.desc())
          .first()
    )
    if not job:
        return None
    job.status = "paused"
    db.add(job); db.commit(); db.refresh(job)
    return job

async def _backend_read_job(printer_id: str) -> dict:
    pid = (printer_id or DEFAULT_PRINTER_ID or "-").strip().lower()
    url = f"{BACKEND_INTERNAL_BASE}/printers/{pid}/octoprint/job?force=1"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url)
            r.raise_for_status()
            return r.json()
    except Exception:
        return {}

# =============================================================================
# Schemas
# =============================================================================
Box = Tuple[float, float, float, float]  # x1,y1,x2,y2

class DetectAlertIn(BaseModel):
    event: Literal["issue_started", "issue_update", "issue_cleared", "issue_resolved"]
    ts: float
    image_url: Optional[str] = None
    video_url: Optional[str] = None
    boxes: List[List[float]] = []
    image_w: Optional[int] = None
    image_h: Optional[int] = None
    printer_id: Optional[str] = None
    source: Optional[str] = "detect_stream"
    recipients: Optional[List[str]] = None
    severity: Optional[Literal["info", "warning", "critical"]] = None
    detected_class: Optional[str] = None
    confidence: Optional[float] = None
    status_text: Optional[str] = None

ALLOWED_FAIL_DETECT_CLASSES = ALLOWED_DETECT_CLASSES

class JobEventIn(BaseModel):
    job_id: int
    status: Literal["completed", "cancelled", "failed"]
    printer_id: Optional[str] = None
    name: Optional[str] = None
    detected_class: Optional[str] = None
    confidence: Optional[float] = None
    finished_at: Optional[datetime] = None

def _status_title_severity(st: str) -> Tuple[str, str]:
    if st == "completed":
        return "🎉 งานพิมพ์เสร็จสิ้น", "info"
    if st == "cancelled":
        return "🚫 งานถูกยกเลิก", "warning"
    return "❌ งานพิมพ์ล้มเหลว", "critical"

def _auto_severity(event: str, conf: float | None) -> str:
    e = (event or "").lower().strip()
    c = float(conf or 0.0)
    if e == "issue_started":
        return "critical" if c >= 0.80 else "warning"
    if e == "issue_update":
        return "warning" if c >= 0.70 else "info"
    if e in ("issue_cleared", "issue_resolved"):
        return "info"
    return "info"

def _normalize_recipients(recipients: list[str] | None) -> list[str]:
    out: list[str] = []
    for r in recipients or []:
        s = (r or "").strip()
        if s:
            out.append(s)
    return out

# =============================================================================
# Detect stream ingest
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

    printer_id = (payload.printer_id or DEFAULT_PRINTER_ID or "-").strip().lower()
    clsname_raw = payload.detected_class or ""
    clsname = _norm_cls(clsname_raw)
    conf = float(payload.confidence or 0.0)
    event = payload.event
    if event == "issue_resolved":
        event = "issue_cleared"

    # push to recent buffer + SSE (always)
    base = {
        "ts": payload.ts,
        "event": event,
        "printer_id": printer_id,
        "detected_class": payload.detected_class,
        "confidence": payload.confidence,
        "image_url": payload.image_url,
        "video_url": payload.video_url,
        "boxes": payload.boxes,
        "image_w": payload.image_w,
        "image_h": payload.image_h,
        "source": payload.source,
    }
    _push_detect(base)
    _spawn(detect_broker.publish(base))

    # clear pending on cleared
    if event == "issue_cleared":
        _clear_pending(printer_id, clsname)

    # ===== 1) bed empty/occupied =====
    if clsname in {"bed_occupied", "bedoccupied", "bed_empty", "bedempty"}:
        if clsname.startswith("bed_empty") or clsname == "bedempty":
            state = "unknown"
            try:
                jobinfo = await _backend_read_job(printer_id)
                mapped = (jobinfo.get("mapped") or {})
                state = (mapped.get("state") or "").strip().lower()
            except Exception:
                pass

            if state in ("printing", "paused"):
                return {"ok": True, "skipped": f"bed_empty while {state}"}

            res2 = await _call_internal(f"/internal/printers/{printer_id}/queue/process-next?force=1")
            ok  = bool((res2 or {}).get("ok"))
            msg = (res2 or {}).get("message", "")
            auto_print_started = ok and msg in ("started", "already-processing", "no-queued")

            if BED_EMPTY_BROADCAST:
                _spawn(unity_ws.broadcast(printer_id, {
                    "event": "bed_empty",
                    "printer_id": printer_id,
                    "ts": datetime.utcnow().timestamp(),
                    "auto_print_started": bool(auto_print_started)
                }))

            return {"ok": True, "bed_empty": True, "auto_print_started": bool(auto_print_started)}

        return {"ok": True, "skipped": f"ignored bed status ({clsname})"}

    # ===== 2) policy check for defects =====
    if event == "issue_update" and not ALERT_ON_UPDATE:
        _spawn(unity_ws.broadcast(printer_id, {
            "issue_active": event != "issue_cleared",
            "event": event,
            "printer_id": printer_id,
            "detected_class": payload.detected_class,
            "confidence": payload.confidence,
            "image_url": payload.image_url,
            "video_url": payload.video_url,
            "ts": payload.ts,
            "severity": "info",
            "title": "Detector update (ignored by policy)",
            "message": f"class={clsname} conf={conf:.2f}",
        }))
        return {"ok": True, "skipped": "update events are disabled"}

    if not clsname:
        _spawn(unity_ws.broadcast(printer_id, {
            "issue_active": event != "issue_cleared",
            "event": event,
            "printer_id": printer_id,
            "detected_class": payload.detected_class,
            "confidence": payload.confidence,
            "image_url": payload.image_url,
            "video_url": payload.video_url,
            "ts": payload.ts,
            "severity": "info",
            "title": "Detector event",
            "message": f"class=? conf={conf:.2f}",
        }))
        return {"ok": True, "skipped": "no detected_class"}

    if event == "issue_cleared":
        if clsname not in ALLOWED_DETECT_CLASSES:
            _spawn(unity_ws.broadcast(printer_id, {
                "issue_active": False,
                "event": event,
                "printer_id": printer_id,
                "detected_class": payload.detected_class,
                "confidence": payload.confidence,
                "image_url": payload.image_url,
                "video_url": payload.video_url,
                "ts": payload.ts,
                "severity": "info",
                "title": "Cleared (ignored by policy)",
                "message": f"class={clsname}",
            }))
            return {"ok": True, "skipped": "cleared for non-allowed class"}
    else:
        if clsname not in ALLOWED_DETECT_CLASSES:
            return {"ok": True, "skipped": f"class '{clsname}' not allowed"}
        if conf < MIN_DETECT_CONFIDENCE:
            return {"ok": True, "skipped": f"low confidence {conf:.2f} < {MIN_DETECT_CONFIDENCE:.2f}"}

    # ===== 2.5) Debounce/Confirm =====
    if DETECT_CONFIRM_ENABLED and event in {"issue_started", "issue_update"}:
        ts_now = _now_ts_fallback(payload.ts)
        st = _add_pending_hit(printer_id, clsname, ts=ts_now, conf=conf, payload=payload.model_dump())
        _gc_pending()
        confirmed, mean_conf = _check_confirm(st)
        if not confirmed:
            _spawn(unity_ws.broadcast(printer_id, {
                "event": "detector_buffering",
                "printer_id": printer_id,
                "detected_class": payload.detected_class,
                "confidence": payload.confidence,
                "hits": st["hits"],
                "mean_conf": round(mean_conf, 3),
                "window_sec": DETECT_CONFIRM_WINDOW_SEC,
                "ts": ts_now,
                "severity": "info",
                "title": "กำลังตรวจสอบความผิดปกติ...",
                "message": f"class={clsname} hits={st['hits']} mean_conf={mean_conf:.2f}",
            }))
            return {"ok": True, "deferred": True, "hits": st["hits"], "mean_conf": mean_conf}
        else:
            _clear_pending(printer_id, clsname)

    # ===== 3) Event defect (ผ่าน confirm/policy แล้ว) =====
    sev = payload.severity or _auto_severity(event, conf)
    title = {
        "issue_started": "🛑 พบความผิดปกติการพิมพ์",
        "issue_update":  "⚠️ อัปเดตความผิดปกติ",
        "issue_cleared": "✅ กลับสู่ปกติ",
    }[event]
    cls_part = f"{payload.detected_class} ({conf:.2f})" if payload.detected_class else "-"
    msg = f"source={payload.source or 'detect_stream'}  printer={printer_id}  class={cls_part}  boxes={len(payload.boxes or [])}"

    data = payload.model_dump()
    data["printer_id"] = printer_id

    # --- auto-pause decision & action ---
    paused_owner: Optional[str] = None
    if AUTO_PAUSE:
        allow_event  = (event in PAUSE_ON_EVENTS)
        allow_class  = (clsname in PAUSE_ON_CLASSES)
        conf_ok      = (conf >= PAUSE_MIN_CONF)
        log.info("[PAUSE] check event=%s class=%s conf=%.2f | allow_event=%s allow_class=%s conf_ok=%s",
                 event, clsname, conf, allow_event, allow_class, conf_ok)

        if allow_event and allow_class and conf_ok:
            await _pause_octoprint()
            job = _pause_current_job_in_db(db, printer_id)
            if job:
                paused_owner = job.employee_id
                logging.info("[PAUSE] DB job#%s -> paused (owner=%s)", job.id, paused_owner or "-")

                # 1) broadcast job_paused (ครั้งเดียว)
                await unity_ws.broadcast(printer_id, {
                    "event": "job_paused",
                    "printer_id": printer_id,
                    "job_id": job.id,
                    "name": job.name,
                    "ts": datetime.utcnow().timestamp(),
                    "severity": "warning",
                    "title": "⏸️ หยุดพิมพ์ชั่วคราว",
                    "message": f"{job.name} • {payload.detected_class} ({conf:.2f})",
                })

                # 2) sticky panel + watchdog
                persist_key = f"pause-{printer_id}"
                panel_payload = {
                    "event": "alert",
                    "printer_id": printer_id,
                    "ts": datetime.utcnow().timestamp(),
                    "severity": "warning",
                    "title": "⏸️ หยุดพิมพ์ชั่วคราว (พบความผิดปกติ)",
                    "message": f"{job.name} • {payload.detected_class} ({conf:.2f})",
                    "ui": {
                        "type": "panel",
                        "variant": "warning",
                        "sticky": True,
                        "timeout_ms": 0,
                        "require_action": True,
                        "persist_key": persist_key,
                        "actions": [
                            {"type": "button", "label": "Resume", "command": "pause", "action": "resume"},
                            {"type": "button", "label": "Cancel", "command": "cancel"}
                        ]
                    }
                }
                asyncio.create_task(_panel_watchdog(printer_id, panel_payload, interval=12.0))

                # 3) แจ้งเจ้าของงาน
                await notify_user(
                    db, job.employee_id,
                    type="print.paused",
                    title="⏸️ หยุดพิมพ์ชั่วคราว (พบความผิดปกติ)",
                    message=f"{job.name} • {payload.detected_class} ({conf:.2f})",
                    severity="warning",
                    data={"job_id": job.id, "printer_id": printer_id, "detected_class": clsname, "confidence": conf},
                )
        else:
            log.info("[PAUSE] skip (policy): event_ok=%s class_ok=%s conf_ok=%s", allow_event, allow_class, conf_ok)

    # broadcast defect event (ธรรมดา)
    _spawn(unity_ws.broadcast(printer_id, {
        "issue_active": event != "issue_cleared",
        "event": event,
        "printer_id": printer_id,
        "detected_class": payload.detected_class,
        "confidence": payload.confidence,
        "image_url": payload.image_url,
        "video_url": payload.video_url,
        "boxes": payload.boxes,
        "image_w": payload.image_w,
        "image_h": payload.image_h,
        "ts": payload.ts,
        "severity": sev,
        "title": title,
        "message": msg,
    }))

    # notify users (priority: payload.recipients -> active job owner -> DEFAULT_ALERT_RECIPIENTS)
    recipients = _normalize_recipients(payload.recipients)
    if not recipients:
        owner = _find_active_owner(db, printer_id)
        if owner:
            recipients = [owner]
    if not recipients and DEFAULT_ALERT_RECIPIENTS:
        recipients = list(DEFAULT_ALERT_RECIPIENTS)

    for emp in recipients:
        if paused_owner and emp == paused_owner:
            continue
        await notify_user(db, emp, type="print_issue", title=title, message=msg, severity=sev, data=data)

    # ถ้า event นี้คือ "issue_cleared" → ปิดพาเนล
    if event == "issue_cleared":
        try:
            await _close_sticky_panel(printer_id, persist_key=f"pause-{printer_id}")
        except Exception:
            pass

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
    if not job:
        raise HTTPException(status_code=404, detail="job_not_found")

    owner = (job.employee_id or "").strip()
    has_owner = bool(owner)

    printer_id = (payload.printer_id or job.printer_id or DEFAULT_PRINTER_ID or "-").strip().lower()
    title, base_sev = _status_title_severity(payload.status)

    cls_raw = payload.detected_class or ""
    cls_norm = _norm_cls(cls_raw)
    conf = float(payload.confidence or 0.0)

    # announce once (long) + dedupe (short)
    if _announced(printer_id, job.id, payload.status):
        return {"ok": True, "skipped": "already_announced"}
    if _should_skip_job_event(printer_id, job.id, payload.status):
        return {"ok": True, "skipped": "duplicate_recent"}

    # cancel/failed → suppress bed_empty→completed ชั่วคราว
    if payload.status in ("cancelled", "failed"):
        _suppress_after_cancel(printer_id)

    extra = ""
    if payload.status == "failed" and cls_norm in ALLOWED_FAIL_DETECT_CLASSES:
        extra = f" • สาเหตุ: {cls_raw} ({conf:.2f})"
    status_tail = f" • {payload.status}"
    msg = f"printer={printer_id} • job_id={job.id} • {job.name or payload.name or '-'}{extra}{status_tail}"

    data = {
        "job_id": job.id,
        "printer_id": printer_id,
        "status": payload.status,
        "name": job.name or payload.name,
        "detected_class": cls_raw or None,
        "confidence": payload.confidence,
        "finished_at": (payload.finished_at or job.finished_at or datetime.utcnow()).isoformat(),
    }

    # Broadcast ไป Unity เสมอ
    evt_map = {"completed": "job_completed", "cancelled": "job_cancelled", "failed": "job_failed"}
    evt = evt_map.get(payload.status, "job_status")
    _spawn(unity_ws.broadcast(printer_id, {
        "event": evt,
        "printer_id": printer_id,
        "job_id": job.id,
        "status": payload.status,
        "title": title,
        "message": msg,
        "severity": base_sev,
        "detected_class": cls_raw or None,
        "confidence": payload.confidence,
        "name": job.name or payload.name,
        "ts": datetime.utcnow().timestamp(),
    }))

    # ปิดพาเนล (ถ้ามี) เมื่อสถานะสิ้นสุดลง
    try:
        await _close_sticky_panel(printer_id, persist_key=f"pause-{printer_id}")
    except Exception:
        pass

    # notify เจ้าของ ถ้ามี
    if has_owner:
        await notify_user(db, owner, type=f"print.{payload.status}", title=title, message=msg, severity=base_sev, data=data)

    _mark_announced(printer_id, job.id, payload.status)
    return {"ok": True, "owner": owner if has_owner else None, "status": payload.status}

# =============================================================================
# RECENT DETECTS: REST + SSE + simple view
# =============================================================================
def _flt_detects(
    *,
    printer_id: str | None,
    detected_class: str | None,
    event: str | None,
    since_ts: float | None,
) -> list[DetectRecord]:
    out: list[DetectRecord] = []
    for rec in reversed(RECENT_DETECTS):
        if printer_id and (rec.printer_id or "") != printer_id:
            continue
        if detected_class and (rec.detected_class or "") != detected_class:
            continue
        if event and (rec.event or "") != event:
            continue
        if since_ts is not None and float(rec.ts or 0) < float(since_ts):
            continue
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
):
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    pid = (printer_id or "").strip().lower() or None
    cls = (detected_class or "").strip().lower() or None
    evt = (event or "").strip() or None
    recs = _flt_detects(printer_id=pid, detected_class=cls, event=evt, since_ts=since_ts)
    return [r.model_dump() for r in recs[:limit]]

@router.get("/detects/stream")
async def stream_detects(request: Request, x_admin_token: str = Header(default="")):
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
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
                    if await request.is_disconnected():
                        break
                    yield ":keepalive\n\n"
        finally:
            detect_broker.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")

@router.get("/detects/view")
def view_detects_page(x_admin_token: str = Header(default="")):
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    html = """
<!doctype html>
<meta charset="utf-8">
<title>Detects Live</title>
<style>
body { font-family: ui-sans-serif, system-ui, -apple-system; line-height:1.4; margin: 16px; }
pre { background:#0b1020; color:#d6ffe8; padding:12px; border-radius:8px; max-height:70vh; overflow:auto; }
small { color:#7a869a; }
code { color:#97e6ff; }
</style>
<h2>Detects (Live)</h2>
<p><small>Streaming from <code>/notifications/detects/stream</code></small></p>
<pre id="log"></pre>
<script>
const log = document.getElementById('log');
const es = new EventSource('/notifications/detects/stream');
es.onmessage = (e) => {
  try {
    const obj = JSON.parse(e.data);
    const t = obj.ts ? new Date(obj.ts*1000).toLocaleTimeString() : new Date().toLocaleTimeString();
    const cls = obj.detected_class || '-';
    const conf = obj.confidence != null ? Number(obj.confidence).toFixed(2) : '-';
    const line = `[${t}] ${obj.event}  ${obj.printer_id || '-'}  ${cls} (${conf})`;
    log.textContent = line + "\\n" + log.textContent;
  } catch (err) {
    log.textContent = e.data + "\\n" + log.textContent;
  }
};
es.onerror = () => {
  log.textContent = "[error] stream disconnected\\n" + log.textContent;
};
</script>
"""
    return HTMLResponse(content=html)

# =============================================================================
# REST: list / create / mark-read (per-user notifications)
# =============================================================================
@router.get("", response_model=list[NotificationOut])
def list_notifications(
    limit: int = 20,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    q = (
        db.query(Notification, NotificationTarget.read_at)
          .join(NotificationTarget, Notification.id == NotificationTarget.notification_id)
          .filter(NotificationTarget.employee_id == current.employee_id)
          .order_by(Notification.id.desc())
          .limit(max(1, min(limit, 100)))
    )
    items: list[NotificationOut] = []
    for n, read_at in q.all():
        items.append(
            NotificationOut.model_validate({
                "id": n.id,
                "ntype": n.ntype,
                "severity": n.severity,
                "title": n.title,
                "message": n.message,
                "data": json.loads(n.data_json) if n.data_json else None,
                "created_at": n.created_at,
                "read": bool(read_at),
            })
        )
    return items

@router.post("", response_model=list[NotificationOut])
async def create_notification(
    payload: NotificationCreate,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    recipients = payload.recipients or [current.employee_id]
    out: list[NotificationOut] = []
    for emp in recipients:
        n = await notify_user(
            db, emp,
            type=payload.type,
            title=payload.title,
            message=payload.message,
            severity=payload.severity,
            data=payload.data or {}
        )
        out.append(
            NotificationOut(
                id=n.id, ntype=n.ntype, severity=n.severity,
                title=n.title, message=n.message,
                data=json.loads(n.data_json) if n.data_json else None,
                created_at=n.created_at, read=False
            )
        )
    return out

@router.post("/mark-read")
def mark_read(
    payload: NotificationMarkRead,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    if not payload.ids:
        return {"ok": True, "updated": 0}
    now = datetime.utcnow()
    updated = (
        db.query(NotificationTarget)
          .filter(
              NotificationTarget.employee_id == current.employee_id,
              NotificationTarget.read_at.is_(None),
          )
          .filter(NotificationTarget.notification_id.in_(payload.ids))
          .update({NotificationTarget.read_at: now}, synchronize_session=False)
    )
    db.commit()
    return {"ok": True, "updated": int(updated or 0)}

@router.post("/mark-all-read")
def mark_all_read(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    updated = (
        db.query(NotificationTarget)
          .filter(
              NotificationTarget.employee_id == current.employee_id,
              NotificationTarget.read_at.is_(None),
          )
          .update({NotificationTarget.read_at: datetime.utcnow()}, synchronize_session=False)
    )
    db.commit()
    return {"ok": True, "updated": int(updated or 0)}

# =============================================================================
# SSE stream (per-user)
# =============================================================================
@router.get("/stream")
async def stream(request: Request, token: str | None = Query(default=None)):
    # ดึง employee_id จาก token
    emp = None
    if token:
        try:
            emp = decode_token(token).get("sub")
        except Exception:
            emp = None
    if not emp:
        authz = request.headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            try:
                emp = decode_token(authz.split(" ", 1)[1]).get("sub")
            except Exception:
                emp = None

    if not emp:
        raise HTTPException(status_code=401, detail="Unauthorized")

    q = await broker.subscribe(emp)

    async def gen():
        yield ":ok\n\n"
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ":keepalive\n\n"
        finally:
            broker.unsubscribe(emp, q)

    return StreamingResponse(gen(), media_type="text/event-stream")

# =============================================================================
# (optional) REST สำหรับสั่งปิดพาเนลจากไคลเอนต์
# =============================================================================
@router.post("/alert/close")
async def close_panel(
    printer_id: str = Query(...),
    persist_key: str = Query(...),
    x_admin_token: str = Header(default="")
):
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    await _close_sticky_panel(printer_id, persist_key=persist_key)
    return {"ok": True}
