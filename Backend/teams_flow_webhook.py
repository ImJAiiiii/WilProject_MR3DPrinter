# backend/teams_flow_webhook.py
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
import random
from typing import Any, Dict, Optional

import requests
from datetime import datetime, timezone, timedelta

# -----------------------------------------------------------------------------#
# Logger
# -----------------------------------------------------------------------------#
log = logging.getLogger("teams_flow_webhook")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(_h)
log.setLevel(logging.INFO)

# -----------------------------------------------------------------------------#
# ENV
# -----------------------------------------------------------------------------#
FLOW_URL: str   = (os.getenv("FLOW_DM_URL") or "").strip()
FLOW_TOKEN: str = (os.getenv("FLOW_DM_TOKEN") or "").strip()

TIMEOUT_S: float    = float(os.getenv("FLOW_DM_TIMEOUT") or "8.0")
MAX_RETRIES: int    = int(os.getenv("FLOW_DM_MAX_RETRIES") or "2")  # total tries = 1 + MAX_RETRIES
BACKOFF_S: float    = float(os.getenv("FLOW_DM_BACKOFF_SEC") or "0.7")
VERIFY_TLS: bool    = os.getenv("FLOW_DM_VERIFY", "1").strip().lower() not in {"0", "false", "no", "off"}
SEND_AUTH_HEADER: bool = os.getenv("FLOW_DM_SEND_AUTH", "0").strip().lower() in {"1", "true", "yes", "on"}

FRONTEND_BASE_URL: str = (os.getenv("FRONTEND_BASE_URL") or "").strip().strip('"').strip("'")
PUBLIC_BASE_URL: str   = (os.getenv("PUBLIC_BASE_URL") or "").strip().strip('"').strip("'")

HTTP_PROXY: Optional[str] = (os.getenv("FLOW_DM_HTTP_PROXY") or "").strip() or None

# -----------------------------------------------------------------------------#
# Utils
# -----------------------------------------------------------------------------#
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL_RE   = re.compile(r"^https?://", re.I)

_BKK_TZ = timezone(timedelta(hours=7))  # UTC+7 (Bangkok)

def _now_bkk_str() -> str:
    """e.g. '30 Oct 2025 08:44'"""
    return datetime.now(timezone.utc).astimezone(_BKK_TZ).strftime("%d %b %Y %H:%M")

def _now_iso() -> str:
    """UTC ISO8601 with Z"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def _valid_email(s: Optional[str]) -> bool:
    return bool(s and _EMAIL_RE.match(s))

def _valid_url(s: Optional[str]) -> bool:
    return bool(s and _URL_RE.match(s.strip()))

def _short(x: Any, n: int = 800) -> str:
    """shorten text/JSON for logging"""
    try:
        s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
        return s if len(s) <= n else s[:n] + "…"
    except Exception:
        return "<unserializable>"

def _redact_secret(s: str, keep: int = 4) -> str:
    if not s:
        return "—"
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "…" + "*" * 6

def _mk_idem_key(job_id: Any, status: str, seed: Optional[str] = None) -> str:
    """Deterministic Idempotency-Key from (job_id, status, seed)"""
    base = f"{job_id}:{status}:{seed or ''}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, base))

def _norm_status(s: Optional[str]) -> str:
    """Normalize status; keep 'processing'"""
    s2 = (s or "").strip().lower()
    if s2.startswith("print."):
        s2 = s2.split(".", 1)[1]

    if s2 in {"queue", "enqueued"}:           return "queued"
    if s2 in {"start"}:                        return "started"
    if s2 in {"processing"}:                   return "processing"
    if s2 in {"complete", "done", "success"}:  return "completed"
    if s2 in {"fail", "error"}:                return "failed"
    if s2 in {"cancel", "cancelled"}:          return "canceled"
    if s2 in {"pause", "stopped"}:             return "paused"
    if s2 in {"problem", "anomaly", "alert"}:  return "issue"

    if s2 in {"queued", "started", "processing", "completed", "failed", "canceled", "paused", "issue"}:
        return s2
    return "issue"

def _norm_severity(sev: Optional[str], status: str) -> str:
    s = (sev or "").strip().lower()
    if s in {"info", "success", "warning", "error", "critical", "neutral"}:
        return s
    st = status.lower()
    if st == "completed": return "success"
    if st == "failed":    return "error"
    if st == "paused":    return "warning"
    if st == "canceled":  return "neutral"
    return "info"

def _as_int_or_zero(v: Any) -> int:
    try:
        return int(v)
    except Exception:
        return 0

def _headers(idem: str, token: str) -> Dict[str, str]:
    h = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "ADI-3DP-Backend/DM",
        "Idempotency-Key": idem,
    }
    if token:
        h["x-flow-token"] = token
        if SEND_AUTH_HEADER:
            h["Authorization"] = f"Bearer {token}"
    return h

def _build_safe_url(url_in: Optional[str]) -> Optional[str]:
    """
    Ensure a valid URL:
      - if url_in is http/https → return it
      - otherwise fallback to PUBLIC_BASE_URL/FRONTEND_BASE_URL + "/#/printing"
    """
    if _valid_url(url_in):
        return url_in.strip()
    for base in (PUBLIC_BASE_URL, FRONTEND_BASE_URL):
        if _valid_url(base):
            return base.rstrip("/") + "/#/printing"
    return None

def _mask_flow_url(u: str) -> str:
    """strip query & mask sig for safer logs"""
    if not u:
        return "—"
    base, _, qs = u.partition("?")
    if "sig=" in qs:
        try:
            sig_val = qs.split("sig=", 1)[1].split("&", 1)[0]
        except Exception:
            sig_val = ""
        masked = qs.replace(sig_val, _redact_secret(sig_val))
        return f"{base}?{masked}"
    return base

# --------------------- detail mappings (EN only) -----------------------------#
_STATUS_EN = {
    "queued":    "Queued",
    "started":   "Starting",
    "processing":"Printing",
    "completed": "Completed",
    "failed":    "Failed",
    "canceled":  "Canceled",
    "paused":    "Paused",
    "issue":     "Issue detected",
}

_REASON_EN = {
    "download_failed":        "Failed to download the G-code file.",
    "octoprint_unreachable":  "Unable to reach OctoPrint (connection timed out).",
    "user_canceled":          "The job was canceled by the user.",
    "status_not_cancelable":  "The job status cannot be canceled.",
}

# strip “(Bangkok time …)” suffix from incoming messages (just in case)
_BKK_SUFFIX_RE = re.compile(r"\s*\(Bangkok time [^)]+\)\s*$", re.I)

def _detail_text_en(status: str, message: str, data: Optional[Dict[str, Any]]) -> str:
    """
    Return a short, user-friendly detail string (English).
    - If status is failed and data.reason exists, map it to a clearer sentence.
    - If message ends with '(Bangkok time ...)', strip it.
    """
    msg = (message or "").strip()
    if msg:
        msg = _BKK_SUFFIX_RE.sub("", msg).strip()

    st = (status or "").lower().strip()
    if st == "failed":
        reason_code = str((data or {}).get("reason") or "").lower().strip()
        if reason_code and reason_code in _REASON_EN:
            return _REASON_EN[reason_code]
        return _STATUS_EN["failed"]

    return _STATUS_EN.get(st, "Status updated")

# -----------------------------------------------------------------------------#
# Main API
# -----------------------------------------------------------------------------#
def notify_dm(
    *,
    employee_email: str,
    status: str,
    job_name: str,
    printer_id: Optional[str] = None,
    job_id: Optional[int | str] = None,
    title: Optional[str] = None,
    message: Optional[str] = None,
    severity: Optional[str] = None,
    url: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    flow_url: Optional[str] = None,
    flow_token: Optional[str] = None,
    timeout_s: Optional[float] = None,
    data: Optional[Dict[str, Any]] = None,   # pass extra info like reason
) -> bool:
    """
    Send DM to Power Automate / Teams:
      - requires employee_email, status, job_name
      - optional: printer_id, job_id, url
      - always includes bangkok_time (UTC+7) for display and created_at (UTC ISO) for systems
      - Idempotency-Key prevents duplicate cards
      - message is converted to concise English detail automatically
    """
    _url   = (flow_url or FLOW_URL).strip()
    _token = (flow_token or FLOW_TOKEN).strip()
    _to    = (employee_email or "").strip()

    if not _url or not _token:
        log.warning("[DM] disabled: FLOW_DM_URL or FLOW_DM_TOKEN is empty")
        return False
    if not _valid_email(_to):
        log.warning("[DM] skip: invalid employee_email=%r", employee_email)
        return False

    st  = _norm_status(status)
    sev = _norm_severity(severity, st)
    jid_int = _as_int_or_zero(job_id) if job_id is not None else 0
    idem = idempotency_key or _mk_idem_key(jid_int, st, seed=job_name or "")

    url_safe = _build_safe_url(url)

    # concise English detail (used as 'message')
    detail_en = _detail_text_en(st, (message or ""), data)

    # NOTE: use "-" for empty fields to avoid bullet rendering in Adaptive Card
    payload: Dict[str, Any] = {
        "employee_email": _to,
        "status": st,                                        # queued|started|processing|completed|failed|canceled|paused|issue
        "title": (title or "ADI 3D Printer Console").strip(),
        "message": detail_en,                                # concise EN detail
        "printer_id": (printer_id or "-").strip(),
        "job_id": jid_int,
        "job_name": job_name or "-",
        "name": job_name or "-",                             # some cards/flows reference 'name'
        "severity": sev,
        "token": _token,                                     # some flows check token in body
        "bangkok_time": _now_bkk_str(),                      # display only; do NOT append to message
        "created_at": _now_iso(),                            # canonical UTC
        "idempotency_key": idem,                             # also included in body for some flows
        "detail_en": detail_en,                              # explicit field if card wants it
        "data": (data or {}),                                # pass-through for debugging
    }
    if url_safe:
        payload["url"] = url_safe

    masked_target = _mask_flow_url(_url)
    log.info(
        "[DM] target=%s send_auth_header=%s verify_tls=%s timeout=%.1fs",
        masked_target,
        SEND_AUTH_HEADER,
        VERIFY_TLS,
        (timeout_s if timeout_s is not None else TIMEOUT_S),
    )

    headers = _headers(idem, _token)
    timeout = (timeout_s if timeout_s is not None else TIMEOUT_S)
    tries = 1 + MAX_RETRIES

    sess = requests.Session()
    if HTTP_PROXY:
        sess.proxies.update({"http": HTTP_PROXY, "https": HTTP_PROXY})

    for attempt in range(1, tries + 1):
        try:
            r = sess.post(
                _url,
                headers=headers,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=timeout,
                verify=VERIFY_TLS,
            )
            if 200 <= r.status_code < 300:
                log.info("[DM] OK -> %s (%s)", _to, _short(payload))
                return True

            body = _short(r.text, 600)
            retryable = (r.status_code in (408, 409, 425, 429)) or (500 <= r.status_code < 600)
            if retryable and attempt < tries:
                sleep_s = BACKOFF_S * attempt + random.uniform(0.0, 0.25)  # jittered backoff
                log.warning("[DM] HTTP %s attempt %d/%d: %s", r.status_code, attempt, tries, body)
                time.sleep(sleep_s)
                continue

            if retryable:
                log.error("[DM] HTTP %s (exhausted): %s", r.status_code, body)
                return False

            log.error("[DM] non-retryable HTTP %s: %s", r.status_code, body)
            return False

        except requests.Timeout as e:
            if attempt < tries:
                log.warning("[DM] timeout attempt %d/%d: %s", attempt, tries, repr(e))
                time.sleep(BACKOFF_S * attempt + random.uniform(0.0, 0.25))
                continue
            log.error("[DM] timeout (exhausted): %s", repr(e))
            return False
        except requests.RequestException as e:
            if attempt < tries:
                log.warning("[DM] request-exception attempt %d/%d: %s", attempt, tries, repr(e))
                time.sleep(BACKOFF_S * attempt + random.uniform(0.0, 0.25))
                continue
            log.error("[DM] request-exception (exhausted): %s", repr(e))
            return False
        except Exception as e:
            log.exception("[DM] fatal error: %s", e)
            return False
    return False

# -----------------------------------------------------------------------------#
# Manual test
# -----------------------------------------------------------------------------#
if __name__ == "__main__":
    os.environ.setdefault("TEST_DM_EMAIL", "someone@example.com")
    ok = notify_dm(
        employee_email=os.getenv("TEST_DM_EMAIL"),
        status="completed",
        job_name="Demo.gcode",
        printer_id="prusa-core-one",
        job_id=123,
        title="ADI 3D Printer Console",
        message='"Demo.gcode" finished on prusa-core-one. (Bangkok time 30 Oct 2025 11:11)',
        severity="success",
        url=None,  # if None, code builds default or omits 'url'
        data={"reason": ""},  # completed → "Completed"
    )
    print("sent:", ok)
