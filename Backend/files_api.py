# backend/files_api.py
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional
from tempfile import SpooledTemporaryFile

import boto3
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from auth import get_confirmed_user
from db import get_db
from models import User
from s3util import new_staging_key  # ให้คีย์ staging/* เป็นรูปแบบเดียวกับระบบหลัก

router = APIRouter(prefix="/api/files", tags=["files"])

# ===================== ENV / CONFIG =====================
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "s3").lower()

S3_ENDPOINT = os.getenv("S3_ENDPOINT")
S3_REGION = os.getenv("S3_REGION", "us-east-1")
S3_BUCKET = os.getenv("S3_BUCKET", "")
S3_SECURE = str(os.getenv("S3_SECURE", "false")).lower() in ("1", "true", "yes")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY")

# จำกัดขนาดไฟล์ (รองรับทั้ง STL และ G-code)
MAX_GCODE_MB = int(os.getenv("MAX_GCODE_MB", "256"))
MAX_GCODE_BYTES = MAX_GCODE_MB * 1024 * 1024

# สำหรับ fallback local (ทางเลือก)
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ===================== Helpers =====================
ALLOWED_EXTS = {"stl", "gcode", "gco", "gc"}


def _ext(name: str) -> str:
    parts = (name or "").lower().rsplit(".", 1)
    return parts[1] if len(parts) == 2 else ""


def _guess_ct(filename: str) -> str:
    e = _ext(filename)
    if e == "stl":
        return "model/stl"
    if e in {"gcode", "gco", "gc"}:
        return "text/x.gcode"
    return "application/octet-stream"


def _is_allowed(filename: str) -> bool:
    return _ext(filename) in ALLOWED_EXTS


def _boto_client():
    return boto3.client(
        "s3",
        region_name=S3_REGION or None,
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        verify=S3_SECURE,  # False = โอเคสำหรับ MinIO dev
    )


# ===================== Routes =====================
@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),  # noqa: ARG001 (อนาคตอาจใช้บันทึกลง DB)
    user: User = Depends(get_confirmed_user),
):
    """
    Legacy upload (ใช้เมื่อ presign PUT ล้มเหลว)
    - รองรับเฉพาะ .stl และ .gcode/.gco/.gc
    - อัปโหลดขึ้น S3/MinIO ที่คีย์ 'staging/<uuid>_<originalname>'
    - คืน 'fileId' = staging key เพื่อให้ FE ไปเรียก /api/storage/upload/complete ต่อ
    """
    orig_name = file.filename or "upload.bin"
    if not _is_allowed(orig_name):
        raise HTTPException(status_code=422, detail="Only STL and G-code are allowed")

    # อ่านแบบสตรีมลงไฟล์ชั่วคราว (ไม่กิน RAM) + บังคับเพดานขนาด
    tmp: SpooledTemporaryFile = SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    total = 0
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_GCODE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large (>{MAX_GCODE_MB}MB)",
                )
            tmp.write(chunk)
        tmp.seek(0)
    finally:
        # ปิดตัว UploadFile เพื่อปล่อย fd/descriptor
        await file.close()

    staging_key = new_staging_key(orig_name)
    content_type = _guess_ct(orig_name)

    if STORAGE_BACKEND == "s3":
        if not S3_BUCKET:
            tmp.close()
            raise HTTPException(status_code=500, detail="S3_BUCKET is not configured")
        try:
            cli = _boto_client()
            # สตรีมขึ้น S3 โดยตรง
            cli.upload_fileobj(
                tmp,
                S3_BUCKET,
                staging_key,
                ExtraArgs={"ContentType": content_type, "ACL": "private"},
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"S3 upload failed: {e}")
        finally:
            tmp.close()
    else:
        # โหมด local: เซฟลงดิสก์
        try:
            subdir = os.path.join(UPLOAD_DIR, "staging")
            os.makedirs(subdir, exist_ok=True)
            local_name = staging_key.split("/", 1)[1] if "/" in staging_key else staging_key
            disk_path = os.path.join(subdir, local_name)
            os.makedirs(os.path.dirname(disk_path), exist_ok=True)
            with open(disk_path, "wb") as f:
                tmp.seek(0)
                while True:
                    buf = tmp.read(1024 * 1024)
                    if not buf:
                        break
                    f.write(buf)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Local save failed: {e}")
        finally:
            tmp.close()

    url: Optional[str] = None
    if STORAGE_BACKEND != "s3":
        # ถ้าเมาท์ StaticFiles ไว้ที่ /uploads
        url = f"/uploads/{staging_key}"

    return JSONResponse(
        {
            "ok": True,
            "fileId": staging_key,  # FE คาดหวังคีย์ staging/* เพื่อนำไป complete
            "filename": orig_name,
            "content_type": content_type,
            "size": total,
            "url": url,
            "uploaded_at": datetime.utcnow().isoformat() + "Z",
        }
    )
