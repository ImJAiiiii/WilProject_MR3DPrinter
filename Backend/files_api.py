# backend/files_api.py
from __future__ import annotations
import os, uuid
from datetime import datetime
from fastapi import APIRouter, UploadFile, File, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from db import get_db
from auth import get_confirmed_user
from models import User

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

router = APIRouter(prefix="/api/files", tags=["files"])

@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_confirmed_user),
):
    ext = os.path.splitext(file.filename)[1].lower()
    uid = uuid.uuid4().hex
    saved_name = f"{uid}{ext or ''}"
    path = os.path.join(UPLOAD_DIR, saved_name)

    # save to disk
    content = await file.read()
    with open(path, "wb") as f:
        f.write(content)

    # public URL served by StaticFiles("/uploads")
    url = f"/uploads/{saved_name}"

    return JSONResponse({
        "ok": True,
        "fileId": saved_name,   # ← frontend ใช้ค่าตัวนี้อ้างถึงไฟล์
        "filename": file.filename,
        "content_type": file.content_type,
        "size": len(content),
        "url": url,
        "uploaded_at": datetime.utcnow().isoformat(),
    })
