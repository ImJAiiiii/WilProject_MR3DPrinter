# file: teams_flow_webhook.py
from __future__ import annotations
import os, time, json, re, logging, requests, uuid
from typing import Any, Dict, Optional

log = logging.getLogger("teams_flow_webhook")

FLOW_URL    = (os.getenv("FLOW_DM_URL") or "").strip()
FLOW_TOKEN  = (os.getenv("FLOW_DM_TOKEN") or "").strip()
TIMEOUT_S   = float(os.getenv("FLOW_DM_TIMEOUT") or "8.0")
MAX_RETRIES = int(os.getenv("FLOW_DM_MAX_RETRIES") or "2")   # รวมครั้งแรก = 1 + MAX_RETRIES
BACKOFF_S   = float(os.getenv("FLOW_DM_BACKOFF_SEC") or "0.7")

# ---- utils --------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def _valid_email(s: Optional[str]) -> bool:
    return bool(s and _EMAIL_RE.match(s))

def _short(s: Any, n: int = 2000) -> str:
    """ป้องกัน payload ยาวเกิน/มี binary; ตัดที่ ~2000 ตัวอักษร"""
    try:
        s = str(s)
        return s if len(s) <= n else (s[:n] + "…")
    except Exception:
        return "<non-str>"

def _mk_idem_key(job_id: Any, status: str, seed: Optional[str] = None) -> str:
    base = f"{job_id}:{status}:{seed or ''}".encode("utf-8", "ignore")
    # ไม่ต้องเข้มงวดมาก แค่ให้ deterministic พอประมาณ; ถ้าอยาก deterministic เต็มๆ ใช้ hashlib.sha1(base).hexdigest()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, base.decode("utf-8", "ignore")))

# ---- main API -----------------------------------------------------------

def notify_dm(
    *,
    employee_email: str,
    status: str,
    job_name: str,
    printer_id: Optional[str] = None,
    job_id: Optional[str | int] = None,
    title: Optional[str] = None,
    message: Optional[str] = None,
    severity: Optional[str] = None,      # "info" | "success" | "warning" | "error"
    url: Optional[str] = None,            # ลิงก์กลับหน้า Printing
    data: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
    flow_url: Optional[str] = None,       # override รายครั้งได้ (ส่วนใหญ่ไม่ต้อง)
    flow_token: Optional[str] = None,     # override รายครั้งได้ (ส่วนใหญ่ไม่ต้อง)
    timeout_s: Optional[float] = None,    # override timeout
) -> bool:
    """
    ยิง HTTP ไปหา Power Automate (HTTP Request trigger) เพื่อ DM ไปยังคนตามอีเมล
    คืน True ถ้าสำเร็จ (HTTP 2xx), False ถ้าล้มเหลวทุกครั้ง
    """
    _url   = (flow_url or FLOW_URL).strip()
    _token = (flow_token or FLOW_TOKEN).strip()
    _to    = (employee_email or "").strip()

    if not _url or not _token:
        log.warning("Flow DM disabled: missing FLOW_DM_URL/FLOW_DM_TOKEN")
        return False
    if not _valid_email(_to):
        log.warning("Flow DM skip: invalid employee_email=%r", employee_email)
        return False

    idem = idempotency_key or _mk_idem_key(job_id or "", status or "", seed=job_name or "")
    payload: Dict[str, Any] = {
        "employee_email": _to,
        "status": (status or "").strip(),
        "title": (title or f"Job {job_name} [{status}]").strip(),
        "message": _short(message or ""),
        "printer_id": printer_id or "",
        "job_id": str(job_id) if job_id is not None else "",
        "job_name": job_name or "",
        "severity": (severity or "info").lower(),
        "url": (url or ""),
        "data": data or {},
        "token": _token,  # ให้ Flow ตรวจเทียบใน Condition
    }

    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "3DP/notify-dm (+teams_flow_webhook)",
        "Idempotency-Key": idem,
    }

    # รีทรายแบบ backoff เฉพาะกรณีชั่วคราว (429/5xx/timeout/connection)
    timeout = (timeout_s if timeout_s is not None else TIMEOUT_S)
    tries = 1 + MAX_RETRIES
    for attempt in range(1, tries + 1):
        try:
            r = requests.post(_url, headers=headers, data=json.dumps(payload), timeout=timeout)
            if 200 <= r.status_code < 300:
                return True

            body = _short(r.text, 800)
            if r.status_code in (408, 409, 425, 429) or 500 <= r.status_code < 600:
                log.warning("Flow DM attempt %s/%s -> %s: %s", attempt, tries, r.status_code, body)
                if attempt < tries:
                    time.sleep(BACKOFF_S * attempt)  # incremental backoff
                    continue
            else:
                log.error("Flow DM non-retryable %s: %s", r.status_code, body)
                return False

        except requests.RequestException as e:
            # เครือข่าย/timeout → รีทรายได้
            log.warning("Flow DM attempt %s/%s error: %s", attempt, tries, repr(e))
            if attempt < tries:
                time.sleep(BACKOFF_S * attempt)
                continue
            return False
        except Exception as e:
            # ผิดพลาดไม่คาดคิด ไม่รีทรายกันลูป
            log.exception("Flow DM fatal: %s", e)
            return False

    return False
