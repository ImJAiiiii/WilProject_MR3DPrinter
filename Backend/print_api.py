# backend/print_api.py
from __future__ import annotations

import os
import re
import httpx
from tempfile import SpooledTemporaryFile
from typing import Optional, Dict, Any, Tuple

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends
from sqlalchemy.orm import Session

from db import get_db
from auth import get_confirmed_user
from models import User

# ===== Helpers for env =====
def _clean_env(v: Optional[str]) -> str:
    return (v or "").strip().strip('"').strip("'")

def _parse_timeout() -> float:
    raw = _clean_env(os.getenv("OCTOPRINT_HTTP_TIMEOUT") or os.getenv("OCTOPRINT_TIMEOUT") or "15")
    m = re.match(r"^\d+(\.\d+)?", raw)
    return float(m.group(0)) if m else 15.0

def _as_bool(v, default=False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return default

# ===== OctoPrint config =====
OCTO_BASE = _clean_env(os.getenv("OCTOPRINT_BASE")).rstrip("/")
OCTO_KEY = _clean_env(os.getenv("OCTOPRINT_API_KEY"))
OCTO_TIMEOUT = _parse_timeout()

# ===== Policy =====
PRINT_MAX_MB = int(os.getenv("PRINT_MAX_MB", "200"))                 # hard limit (MB)
SPOOL_MAX_BYTES = min(PRINT_MAX_MB, 200) * 1024 * 1024               # cap RAM before spill to disk
ALLOWED_EXTS = {".gcode", ".gco", ".gc"}                             # common G-code extensions
GCODE_MIME = "text/x.gcode"                                          # บอกตรงๆว่าเป็น G-code

router = APIRouter(prefix="/octo", tags=["print"])  # ⚠️ ไม่ชน /api/print ของระบบคิว

def _octo_ready() -> bool:
    return bool(OCTO_BASE and OCTO_KEY and OCTO_BASE.startswith(("http://", "https://")))

def _octo_headers() -> Dict[str, str]:
    # httpx จะตั้ง multipart boundary ให้เอง; ไม่ต้องกำหนด Content-Type
    return {"X-Api-Key": OCTO_KEY, "Accept": "application/json"}

SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

def _safe_filename(name: str, fallback: str = "job.gcode") -> str:
    """
    ทำความสะอาดชื่อไฟล์ + บังคับให้อยู่ในชุดนามสกุลที่อนุญาต
    """
    name = (name or "").strip() or fallback
    name = SAFE_FILENAME_RE.sub("_", name)
    base, ext = os.path.splitext(name)
    if not ext:
        ext = ".gcode"
    ext_l = ext.lower()
    if ext_l not in ALLOWED_EXTS:
        # ปฏิเสธทันทีเพื่อหลีกเลี่ยงอัปโหลดไฟล์อื่นเข้า OctoPrint
        raise HTTPException(422, f"Unsupported file extension: {ext}")
    # ลด '_' ซ้ำซ้อน และป้องกันชื่อยาวเกินเหตุ
    cleaned = re.sub(r"_+", "_", f"{base}{ext}")
    return cleaned[:180]  # กันชื่อยาวมากไป

async def _download_to_spooled(url: str, timeout: float) -> Tuple[SpooledTemporaryFile, int]:
    """
    Stream-download URL -> SpooledTemporaryFile (บังคับขนาดสูงสุด).
    """
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(422, "file_url must be http(s)")
    total = 0
    spooled = SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES, mode="w+b")
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", url) as r:
                r.raise_for_status()
                async for chunk in r.aiter_bytes():
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > PRINT_MAX_MB * 1024 * 1024:
                        raise HTTPException(413, f"File too large (>{PRINT_MAX_MB} MB)")
                    spooled.write(chunk)
        spooled.seek(0)
        return spooled, total
    except HTTPException:
        spooled.close()
        raise
    except httpx.HTTPStatusError as e:
        spooled.close()
        # อธิบายเหตุผลให้อ่านง่าย (เช่น 404 จาก presign หมดอายุ)
        detail = f"Fetch failed: HTTP {e.response.status_code}"
        try:
            j = e.response.json()
            if isinstance(j, dict):
                msg = j.get("error") or j.get("message")
                if msg:
                    detail += f" - {msg}"
        except Exception:
            pass
        raise HTTPException(e.response.status_code, detail)
    except Exception as e:
        spooled.close()
        raise HTTPException(502, f"Fetch failed: {e}")

async def _upload_to_octoprint(file_tuple, *, location: str, select: bool, auto_print: bool) -> Any:
    """
    file_tuple = (filename, fileobj, mime)
    select/print ใส่ผ่าน querystring เพื่อความชัดเจน (OctoPrint รองรับแน่นอน)
    """
    if location not in ("local", "sdcard"):
        raise HTTPException(422, "location must be 'local' or 'sdcard'")
    qs = f"select={'true' if select else 'false'}&print={'true' if auto_print else 'false'}"
    url = f"{OCTO_BASE}/api/files/{location}?{qs}"
    async with httpx.AsyncClient(timeout=OCTO_TIMEOUT, follow_redirects=True) as client:
        res = await client.post(
            url,
            headers=_octo_headers(),
            files={"file": file_tuple},  # multipart/form-data
        )
        # 204 = success without body (selected/started)
        if res.status_code == 204:
            return {"status": 204, "detail": "Uploaded and action performed"}
        # parse JSON เมื่อเป็นไปได้
        try:
            payload = res.json()
        except Exception:
            payload = {"status": res.status_code, "detail": res.text[:500]}
        if not res.is_success:
            raise HTTPException(res.status_code, f"OctoPrint: {payload.get('error') or payload.get('detail') or res.text}")
        return payload

@router.post("/print-now")
async def print_now(
    # Which printer (informational for multi-printer setup)
    printer_id: Optional[str] = Form(None),

    # Option A: presigned GET URL ของ .gcode (หรือ URL อื่นที่ดึงได้)
    file_url: Optional[str] = Form(None),
    filename: Optional[str] = Form(None),

    # Option B: direct upload
    file: Optional[UploadFile] = File(None),

    # OctoPrint settings
    location: str = Form("local"),           # local | sdcard
    select: Optional[bool | str] = Form(True),
    auto_print: Optional[bool | str] = Form(True),

    db: Session = Depends(get_db),
    me: User = Depends(get_confirmed_user),
):
    """
    ทางลัด (debug/admin): อัปโหลด G-code ไป OctoPrint แล้วสั่งพิมพ์ทันที

    **หมายเหตุ (Production)**:
    - ปกติควรเข้าคิวผ่าน /api/print ของระบบคิว แล้ว worker ค่อยเรียกจุดนี้เมื่อถึงคิว
    - Endpoint นี้จะ "ไม่" ยอมรับไฟล์ที่ไม่ใช่ G-code ตาม ALLOWED_EXTS
    """
    if not _octo_ready():
        raise HTTPException(503, "OctoPrint is not configured (check OCTOPRINT_BASE / OCTOPRINT_API_KEY)")

    select_flag = _as_bool(select, True)
    print_flag = _as_bool(auto_print, True)

    file_to_close: Optional[SpooledTemporaryFile] = None
    total: Optional[int] = None
    safe_name = None

    try:
        if file is not None:
            # --- Direct upload ---
            safe_name = _safe_filename(filename or file.filename or "job.gcode")
            # stream UploadFile -> spooled (enforce size)
            total = 0
            spooled = SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES, mode="w+b")
            file_to_close = spooled
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > PRINT_MAX_MB * 1024 * 1024:
                    raise HTTPException(413, f"File too large (>{PRINT_MAX_MB} MB)")
                spooled.write(chunk)
            spooled.seek(0)
            result = await _upload_to_octoprint(
                (safe_name, spooled, GCODE_MIME),
                location=location,
                select=select_flag,
                auto_print=print_flag,
            )

        elif file_url:
            # --- Download from URL, then upload to OctoPrint ---
            # ถ้า caller ระบุชื่อไฟล์มา จะใช้ validate เพื่อกันไฟล์ปลอม
            safe_name = _safe_filename(filename or "job.gcode")
            spooled, total = await _download_to_spooled(file_url, OCTO_TIMEOUT)
            file_to_close = spooled
            result = await _upload_to_octoprint(
                (safe_name, spooled, GCODE_MIME),
                location=location,
                select=select_flag,
                auto_print=print_flag,
            )

        else:
            raise HTTPException(422, "Need file (upload) or file_url")

        return {
            "ok": True,
            "printer_id": printer_id,
            "location": location,
            "selected": select_flag,
            "started": print_flag,
            "filename": safe_name,
            "size_bytes": total,
            "octoprint": result,
            "user": {"id": me.id, "employee_id": me.employee_id, "name": me.name},
        }

    except httpx.HTTPStatusError as e:
        # คว้า body จาก OctoPrint มาอธิบายเพิ่มถ้าเป็นไปได้
        detail = f"OctoPrint HTTP {e.response.status_code}"
        try:
            j = e.response.json()
            if isinstance(j, dict):
                msg = j.get("error") or j.get("message")
                if msg:
                    detail += f" - {msg}"
        except Exception:
            pass
        raise HTTPException(e.response.status_code, detail)

    except HTTPException:
        raise

    except Exception as e:
        raise HTTPException(502, f"Create print failed: {e}")

    finally:
        # ปิด resource ให้เรียบร้อยเสมอ
        try:
            if file is not None:
                await file.close()  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            if file_to_close:
                file_to_close.close()
        except Exception:
            pass
