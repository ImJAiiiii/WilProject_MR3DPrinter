# backend/storage.py
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime

from db import get_db
from auth import get_confirmed_user, get_current_user, get_manager_user
from models import StorageFile, User
from s3util import new_object_key, presign_put, presign_get, delete_object

router = APIRouter(prefix="/storage", tags=["storage"])
    
@router.post("/upload/request")
def request_upload(filename: str, content_type: Optional[str] = None, size: Optional[int] = None,
                   db: Session = Depends(get_db),
                   user: User = Depends(get_confirmed_user)):
    key = new_object_key(filename)
    signed = presign_put(key, content_type)
    return signed  # {method,url,headers,expires_in,object_key}

@router.post("/upload/complete")
def complete_upload(object_key: str, filename: str,
                    content_type: Optional[str] = None, size: Optional[int] = None,
                    etag: Optional[str] = None,
                    db: Session = Depends(get_db),
                    user: User = Depends(get_confirmed_user)):
    row = StorageFile(
        employee_id=user.employee_id,
        filename=filename,
        object_key=object_key,
        content_type=content_type,
        size=size,
        etag=(etag or "").strip('"'),
        uploaded_at=datetime.utcnow(),
    )
    db.add(row); db.commit(); db.refresh(row)
    return {
        "id": row.id, "employee_id": row.employee_id, "filename": row.filename,
        "object_key": row.object_key, "content_type": row.content_type,
        "size": row.size, "uploaded_at": row.uploaded_at.isoformat(),
    }

@router.get("")
def list_files(limit: int = 50, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    q = (db.query(StorageFile)
            .order_by(StorageFile.uploaded_at.desc())
            .limit(max(1, min(limit, 200))))
    rows = q.all()
    return [
        {
            "id": r.id,
            "filename": r.filename,
            "object_key": r.object_key,
            "size": r.size,
            "content_type": r.content_type,
            "uploaded_at": r.uploaded_at.isoformat(),
            "owner": r.employee_id,
        }
        for r in rows
    ]

@router.get("/{fid}/download")
def download_file(fid: int, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    r = db.query(StorageFile).filter(StorageFile.id == fid).first()
    if not r: raise HTTPException(404, "Not found")
    return {"url": presign_get(r.object_key)}

@router.delete("/{fid}")
def delete_file(fid: int, db: Session = Depends(get_db),
                manager: User = Depends(get_manager_user)):
    r = db.query(StorageFile).filter(StorageFile.id == fid).first()
    if not r: return {"ok": True}
    try: delete_object(r.object_key)
    except Exception: pass
    db.delete(r); db.commit()
    return {"ok": True}
