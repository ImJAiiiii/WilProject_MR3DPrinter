# backend/custom_storage_s3.py
from __future__ import annotations

import os
import mimetypes
import re

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Header
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user, get_confirmed_user, get_manager_user
from models import StorageFile, User
from schemas import (
    StorageUploadRequestIn, StorageUploadRequestOut,
    StorageUploadCompleteIn, StorageFileOut, StorageUploaderOut,
)
from s3util import (
    new_staging_key, new_storage_key,
    presign_put, presign_get, head_object,
    delete_object, copy_object,
)

import boto3
from botocore.config import Config

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
WHITELIST_PREFIX = ("storage/", "catalog/", "printer-store/")

router = APIRouter(prefix="/storage", tags=["storage"])

_S3_BUCKET      = os.getenv("S3_BUCKET", "printer-store")
_S3_ENDPOINT    = os.getenv("S3_ENDPOINT")       # เช่น "http://127.0.0.1:9000"
_S3_REGION      = os.getenv("S3_REGION", "auto")
_S3_ACCESS_KEY  = os.getenv("S3_ACCESS_KEY")
_S3_SECRET_KEY  = os.getenv("S3_SECRET_KEY")

def _s3_client():
    if not (_S3_BUCKET and _S3_ENDPOINT and _S3_ACCESS_KEY and _S3_SECRET_KEY):
        raise RuntimeError("S3 not configured")
    return boto3.client(
        "s3",
        endpoint_url=_S3_ENDPOINT,
        aws_access_key_id=_S3_ACCESS_KEY,
        aws_secret_access_key=_S3_SECRET_KEY,
        region_name=_S3_REGION,
        config=Config(s3={"addressing_style": "path"}),
    )

def _mime_for(key: str) -> str:
    ct = _guess_ct(key)
    return ct or "application/octet-stream"

# ---------- helpers ----------
def _uploader(u: Optional[User], fallback_emp: Optional[str] = None) -> StorageUploaderOut:
    return StorageUploaderOut(
        employee_id=(u.employee_id if u else (fallback_emp or "")),
        name=(u.name if u else None),
        email=(u.email if u else None),
    )

def _validate_key(key: str) -> str:
    k = (key or "").strip().lstrip("/")  # เพิ่ม lstrip("/")
    if not k or k.startswith("/") or ".." in k or "://" in k:
        raise HTTPException(400, "invalid object_key")
    return k

def _guess_ct(name: str, default: str = "application/octet-stream") -> str:
    lower = (name or "").lower()
    if lower.endswith((".gcode", ".gc", ".gco")):
        return "text/plain"
    if lower.endswith(".stl"):
        return "model/stl"
    if lower.endswith(".3mf"):
        return "application/vnd.ms-package.3dmanufacturing-3dmodel"
    if lower.endswith(".obj"):
        return "text/plain"
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    ct, _ = mimetypes.guess_type(name)
    return ct or default

def _to_out(db: Session, row: StorageFile) -> StorageFileOut:
    try:
        url = presign_get(row.object_key)
    except Exception:
        url = None
    u = db.query(User).filter(User.employee_id == row.employee_id).first()
    return StorageFileOut(
        id=row.id,
        filename=row.filename,
        object_key=row.object_key,
        content_type=row.content_type,
        size=row.size,
        uploaded_at=row.uploaded_at,
        url=url,
        uploader=_uploader(u, fallback_emp=row.employee_id),
    )

# ---------- upload (presigned PUT → staging/*) ----------

@router.post("/upload/request", response_model=StorageUploadRequestOut)
def request_upload(
    payload: StorageUploadRequestIn,
    db: Session = Depends(get_db),  # noqa: ARG001 (เผื่ออนาคตเก็บรอย)
    me: User = Depends(get_confirmed_user),
):
    filename = payload.filename or "upload.bin"
    ct = payload.content_type or _guess_ct(filename)
    key = new_staging_key(filename)  # staging/<uuid>/<filename>
    signed = presign_put(key, ct)
    return StorageUploadRequestOut(
        object_key=signed["object_key"],
        method=signed.get("method", "PUT"),
        url=signed["url"],
        headers=signed.get("headers", {}),
        expires_in=int(signed.get("expires_in", 600)),
    )

# ---------- upload/complete (legacy: เขียน DB ตาม key ที่อัพแล้ว) ----------

@router.post("/upload/complete", response_model=StorageFileOut)
def complete_upload(
    payload: StorageUploadCompleteIn,
    db: Session = Depends(get_db),
    me: User = Depends(get_confirmed_user),
):
    key = _validate_key(payload.object_key)
    size = payload.size
    content_type = payload.content_type or _guess_ct(payload.filename or key)
    try:
        meta = head_object(key)
        size = size or int(meta.get("ContentLength", 0))
        content_type = meta.get("ContentType") or content_type
    except Exception:
        pass

    row = StorageFile(
        employee_id=me.employee_id,
        filename=payload.filename or os.path.basename(key),
        object_key=key,
        content_type=content_type,
        size=size,
        uploaded_at=datetime.utcnow(),
    )
    db.add(row); db.commit(); db.refresh(row)
    return _to_out(db, row)

# ---------- finalize (staging → storage + บันทึก DB + ลบ staging) ----------

@router.post("/finalize", response_model=StorageFileOut)
def finalize_to_storage(
    payload: StorageUploadCompleteIn,  # ใช้: object_key, filename, content_type?, size?
    db: Session = Depends(get_db),
    me: User = Depends(get_confirmed_user),
):
    src_key = _validate_key(payload.object_key)
    if not src_key.startswith("staging/"):
        raise HTTPException(400, "object_key must be under staging/")
    dst_name = payload.filename or os.path.basename(src_key)
    dst_key = new_storage_key(dst_name)
    content_type = payload.content_type or _guess_ct(dst_name)

    try:
        # คัดลอกพร้อมตั้ง Content-Type ปลายทางให้ถูกต้อง
        copy_object(src_key, dst_key, content_type=content_type)
    except Exception as e:
        raise HTTPException(500, f"failed to copy to storage: {e}")

    size = payload.size
    try:
        meta = head_object(dst_key)
        size = size or int(meta.get("ContentLength", 0))
        content_type = meta.get("ContentType") or content_type
    except Exception:
        pass

    row = StorageFile(
        employee_id=me.employee_id,
        filename=os.path.basename(dst_key),
        object_key=dst_key,
        content_type=content_type,
        size=size,
        uploaded_at=datetime.utcnow(),
    )
    db.add(row); db.commit(); db.refresh(row)

    try:
        delete_object(src_key)
    except Exception:
        pass

    return _to_out(db, row)

# ---------- history (mine / by user / all) ----------

@router.get("/my", response_model=List[StorageFileOut])
def list_my_files(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    rows = (
        db.query(StorageFile)
        .filter(StorageFile.employee_id == me.employee_id)
        .order_by(StorageFile.uploaded_at.desc(), StorageFile.id.desc())
        .limit(limit)
        .all()
    )
    return [_to_out(db, r) for r in rows]

@router.get("/by-user/{employee_id}", response_model=List[StorageFileOut])
def list_by_user(
    employee_id: str,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _manager: User = Depends(get_manager_user),
):
    emp = str(employee_id).strip()
    rows = (
        db.query(StorageFile)
        .filter(StorageFile.employee_id == emp)
        .order_by(StorageFile.uploaded_at.desc(), StorageFile.id.desc())
        .limit(limit)
        .all()
    )
    return [_to_out(db, r) for r in rows]

@router.get("", response_model=List[StorageFileOut])
def list_files(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _manager: User = Depends(get_manager_user),
):
    rows = (
        db.query(StorageFile)
        .order_by(StorageFile.uploaded_at.desc(), StorageFile.id.desc())
        .limit(limit)
        .all()
    )
    return [_to_out(db, r) for r in rows]

# ---------- get / delete / presign / head ----------

@router.get("/id/{fid}", response_model=StorageFileOut)
def get_file(
    fid: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    row = db.query(StorageFile).filter(StorageFile.id == fid).first()
    if not row:
        raise HTTPException(404, "Not found")

    if (row.employee_id != me.employee_id) and (not getattr(me, "can_manage_queue", False)):
        raise HTTPException(403, "forbidden")

    return _to_out(db, row)

@router.delete("/id/{fid}")
def delete_file(
    fid: int,
    db: Session = Depends(get_db),
    _manager: User = Depends(get_manager_user),
):
    row = db.query(StorageFile).filter(StorageFile.id == fid).first()
    if not row:
        return {"ok": True}
    try:
        delete_object(row.object_key)
    except Exception:
        pass
    db.delete(row); db.commit()
    return {"ok": True}

@router.get("/presign")
def presign_download(
    object_key: str = Query(..., description="S3 object key"),
    with_meta: bool = Query(False, description="return metadata as well"),
    x_admin_token: str | None = Header(default=None),  # ไม่ต้อง auth DB
):
    # (ตัวเลือก) เปิดการ์ด token ภายใน
    # if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
    #     raise HTTPException(401, "Unauthorized")

    key = _validate_key(object_key)

    # อนุญาตเฉพาะ key ใต้ prefix ที่ปลอดภัย (กัน path แปลก)
    if not key.startswith(WHITELIST_PREFIX):
        raise HTTPException(403, "forbidden_prefix")

    try:
        url = presign_get(key)  # ไม่แตะ DB
    except Exception as e:
        raise HTTPException(500, f"Failed to generate presigned url: {e}")

    if not with_meta:
        return {"url": url}

    meta = {}
    try:
        h = head_object(key)  # อ่านตรงจาก S3
        meta = {
            "content_type": h.get("ContentType"),
            "size": int(h.get("ContentLength", 0)),
            "etag": h.get("ETag"),
            "last_modified": h.get("LastModified").isoformat() if h.get("LastModified") else None,
        }
    except Exception:
        pass

    return {"url": url, "meta": meta}

@router.get("/head")
def head(
    object_key: str = Query(..., description="S3 object key"),
    _me: User = Depends(get_current_user),
):
    key = _validate_key(object_key)
    try:
        h = head_object(key)
        return {
            "object_key": key,
            "content_type": h.get("ContentType"),
            "size": int(h.get("ContentLength", 0)),
            "etag": h.get("ETag"),
            "last_modified": h.get("LastModified").isoformat() if h.get("LastModified") else None,
        }
    except Exception as e:
        raise HTTPException(404, f"Object not found: {e}")

@router.get("/all")
def list_all_s3(
    prefix: str = Query("catalog/", description="S3 key prefix to list"),
    limit: int = Query(2000, ge=1, le=5000),
    _me: User = Depends(get_confirmed_user),  # ต้องล็อกอิน แต่ไม่ต้องเป็น manager
):
    """
    List ALL objects under the given S3 prefix (default: 'catalog/').
    Shape: {"ok": true, "items":[{"object_key", "content_type","size","owner", "uploaded_at"}], "prefix": "...", "count": N}
    """
    try:
        s3 = _s3_client()
    except Exception as e:
        raise HTTPException(503, f"S3 not configured: {e}")

    items: List[dict] = []
    token: Optional[str] = None
    fetched = 0

    while True:
        kwargs = dict(Bucket=_S3_BUCKET, Prefix=prefix, MaxKeys=min(1000, max(1, limit - fetched)))
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)

        for obj in resp.get("Contents", []):
            key = obj.get("Key") or ""
            if not key or key.endswith("/"):
                continue  # skip pseudo-folders
            items.append({
                "object_key": key,
                "content_type": _mime_for(key),
                "size": int(obj.get("Size") or 0),
                "owner": None,  # unknown at list time (ไม่ผูกกับ user)
                "uploaded_at": obj.get("LastModified").isoformat() if obj.get("LastModified") else None,
            })
            fetched += 1
            if fetched >= limit:
                break

        if fetched >= limit:
            break
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")

    return {"ok": True, "items": items, "prefix": prefix, "count": len(items)}

# ======= CATALOG LISTING + HELPERS ===========================================

def _derive_model_from_key(key: str) -> Optional[str]:
    try:
        parts = (key or "").split("/")
        return parts[1] if len(parts) >= 2 and parts[0] == "catalog" else None
    except Exception:
        return None

def _basename(key: str) -> str:
    import os as _os
    return _os.path.basename(key or "")

def _ext(name: str) -> str:
    n = (name or "").lower()
    i = n.rfind(".")
    return n[i+1:] if i >= 0 else ""

def _is_gcode_name(name: str) -> bool:
    e = _ext(name)
    return e in ("gcode", "gco", "gc")

@router.get("/catalog")
def storage_catalog(
    model: Optional[str] = Query(None, description="เช่น Hontech, Delta"),
    q: Optional[str] = Query(None, description="คำค้นในชื่อไฟล์"),
    offset: int = Query(0, ge=0),
    limit: int = Query(2000, ge=1, le=5000),
    with_urls: int = Query(1, description="แนบ presigned url ให้รูป preview ถ้ามี"),
    with_head: int = Query(0),
    include_staging: int = Query(0),  # เผื่อ FE ส่งมา ไม่ได้ใช้
    _me: User = Depends(get_confirmed_user),
):
    """
    ส่งรายการไฟล์ใน S3 ภายใต้ prefix 'catalog/'. รูปร่างแต่ละชิ้นจะสอดคล้องกับที่ FE คาดหวัง:
    { display_name, filename, object_key, model, size, content_type, ext,
      uploaded_at, thumb, preview_url, json_key, stats?, uploader? }
    """
    try:
        s3 = _s3_client()
    except Exception as e:
        raise HTTPException(503, f"S3 not configured: {e}")

    prefix = "catalog/" if not model else f"catalog/{model.strip().rstrip('/')}/"
    fetched = 0
    token: Optional[str] = None

    # ดึง object ทั้ง prefix มาก่อน เพื่อรู้ว่ามี preview/json คู่กันหรือไม่
    objects: dict[str, dict] = {}
    keys_present: set[str] = set()

    while True:
        kwargs = dict(Bucket=_S3_BUCKET, Prefix=prefix, MaxKeys=min(1000, 5000 - fetched))
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)

        for obj in resp.get("Contents", []):
            key = obj.get("Key") or ""
            if not key or key.endswith("/"):
                continue
            keys_present.add(key)
            objects[key] = obj
            fetched += 1
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    
    def _preview_candidates(gkey: str) -> list[str]:
        base = gkey.rsplit(".", 1)[0]
        dirp = os.path.dirname(gkey)
        name = os.path.basename(base)

        # ตัด suffix ที่มักจะมีในชื่อ G-code แต่รูปพรีวิวไม่มี
        # เช่น ..._oriented / -oriented / oriented (มี/ไม่มีตัวคั่น)
        name_no_oriented = re.sub(r'([_\-\s]?oriented)$', '', name, flags=re.IGNORECASE)

        cands = [
            f"{base}.preview.png",
            f"{base}_preview.png",
            f"{base}_oriented_preview.png",
            f"{dirp}/{name}.preview.png",
            f"{dirp}/{name}_preview.png",
        ]

        if name_no_oriented and name_no_oriented != name:
            cands += [
                f"{dirp}/{name_no_oriented}.preview.png",
                f"{dirp}/{name_no_oriented}_preview.png",
            ]

        # unique & คงลำดับ
        seen = set(); out = []
        for p in cands:
            if p not in seen:
                seen.add(p); out.append(p)
        return out

    items: list[dict] = []
    text_q = (q or "").strip().lower()

    for key, meta in objects.items():
        name = _basename(key)
        if not _is_gcode_name(name):
            continue  # แสดงเฉพาะ gcode เป็น "ไฟล์หลัก"

        if text_q and text_q not in name.lower():
            # กรองด้วยคำค้นแบบง่าย ๆ
            continue

        # หา preview/json ที่อยู่คู่กัน
        cands = _preview_candidates(key)
        preview_key = next((p for p in cands if p in keys_present), None)
        json_key = f"{key.rsplit('.',1)[0]}.json"
        if json_key not in keys_present:
            json_key = None

        # สร้าง record
        uploaded_at = meta.get("LastModified").isoformat() if meta.get("LastModified") else None
        size = int(meta.get("Size") or 0)
        content_type = _mime_for(key)
        ext_str = _ext(name)
        model_name = _derive_model_from_key(key)

        rec = {
            "display_name": name,
            "filename": name,
            "object_key": key,
            "gcode_key": key,
            "model": model_name,
            "size": size,
            "content_type": content_type,
            "ext": ext_str,
            "uploaded_at": uploaded_at,
            # FE รองรับ 2 แบบ: ถ้าเป็น URL เต็มให้ field 'preview_url' เป็น https, ถ้าเป็น key ให้ส่ง key
            "preview_url": preview_key or None,
            "thumb": preview_key or None,     # เผื่อ FE ใช้ field นี้
            "json_key": json_key or None,
            "uploader": None,                 # ไม่มีข้อมูลผูก user ใน S3 ตรงนี้
            "stats": None,                    # ให้ modal/manifest เติมทีหลัง
        }

        if with_urls and preview_key:
            try:
                rec["preview_url"] = presign_get(preview_key)
            except Exception:
                # ถ้า presign ไม่ได้ก็ปล่อยเป็น key ให้ FE ไปตีเป็น /files/raw เอง
                rec["preview_url"] = preview_key

        if with_head:
            # แนบ head เพิ่มถ้าขอมา
            try:
                h = head_object(key)
                rec["size"] = int(h.get("ContentLength") or size)
                rec["content_type"] = h.get("ContentType") or content_type
            except Exception:
                pass

        items.append(rec)

    # offset/limit ฝั่งเซิร์ฟเวอร์
    items.sort(key=lambda r: r.get("uploaded_at") or "", reverse=True)
    if offset:
        items = items[offset:]
    if limit:
        items = items[:limit]

    return {"ok": True, "items": items, "count": len(items)}

# ======= RANGE READER (tail of G-code) =======================================

from fastapi.responses import PlainTextResponse

@router.get("/range", response_class=PlainTextResponse)
def get_range_text(
    object_key: str = Query(..., description="S3 object key ของไฟล์ G-code"),
    start: int = Query(-400000, description="เริ่ม byte; ค่าลบ = นับจากท้ายไฟล์"),
    length: int = Query(400000, description="จำนวน byte ที่อ่าน (สูงสุด ~4MB)"),
    _me: User = Depends(get_confirmed_user),
):
    key = _validate_key(object_key)
    try:
        s3 = _s3_client()
    except Exception as e:
        raise HTTPException(503, f"S3 not configured: {e}")

    # คำนวณช่วง byte
    try:
        h = s3.head_object(Bucket=_S3_BUCKET, Key=key)
        total = int(h.get("ContentLength") or 0)
    except Exception as e:
        raise HTTPException(404, f"Object not found: {e}")

    if total <= 0:
        return PlainTextResponse("", media_type="text/plain")

    if start < 0:
        begin = max(total + start, 0)
    else:
        begin = max(start, 0)

    end = min(begin + max(length, 1) - 1, total - 1)
    byte_range = f"bytes={begin}-{end}"

    try:
        obj = s3.get_object(Bucket=_S3_BUCKET, Key=key, Range=byte_range)
        data = obj["Body"].read()
        # พยายาม decode เป็น utf-8 ถ้าไม่ได้ก็ใช้ latin-1
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = data.decode("latin-1", errors="replace")
        return PlainTextResponse(text, media_type="text/plain")
    except Exception as e:
        raise HTTPException(500, f"Failed to read range: {e}")

# ======= DELETE BY KEY (สำหรับ FE fallback) ==================================

from fastapi import Body

@router.delete("/by-key")
def delete_by_key(
    payload: dict = Body(..., description='{"object_key": "...", "delete_object_from_s3": true}'),
    _manager: User = Depends(get_manager_user),
):
    key = _validate_key(payload.get("object_key", ""))
    try:
        delete_object(key)
    except Exception:
        # ถ้าลบไม่ได้ก็ไม่ต้อง fail hard
        pass
    return {"ok": True, "deleted": key}
