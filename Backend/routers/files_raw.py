# backend/routers/files_raw.py
from __future__ import annotations

import os
import mimetypes
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Depends
from fastapi.responses import StreamingResponse, FileResponse, Response

# ✅ ตรวจ token ได้ทั้ง Header และ query (?token=)
from auth import get_user_from_header_or_query

# ---------------- Boot mimetypes ----------------
# บางเครื่องจะไม่รู้จักนามสกุลเหล่านี้ → ใส่เองให้แน่นอน
mimetypes.add_type("text/x.gcode", ".gcode")
mimetypes.add_type("text/x.gcode", ".gco")
mimetypes.add_type("text/x.gcode", ".gc")
mimetypes.add_type("model/stl", ".stl")
mimetypes.add_type("image/png", ".preview.png")  # เผื่อ OS ไม่รู้จักนามสกุลพิเศษ

# ---------------- Config ----------------
BACKEND_DIR = Path(__file__).resolve().parent
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "s3").lower()
UPLOADS_DIR = os.getenv("UPLOADS_DIR", "uploads")
UPLOADS_DIR_ABS = str((BACKEND_DIR / UPLOADS_DIR).resolve())

router = APIRouter(prefix="/files", tags=["files"])

# ---------------- Normalizer ----------------
def _norm_object_key(k: Optional[str]) -> str:
    """
    แก้ปัญหาคีย์ที่มี / นำหน้า หรือมี backslash และ // ซ้อน
    เช่น '%2Fimages%2F3D.png' -> '/images/3D.png' -> 'images/3D.png'
    * FastAPI จะ decode %2F ให้แล้ว ดังนั้นนี่คือการ clean path
    """
    k = (k or "").strip()
    k = k.replace("\\", "/").lstrip("/")
    while "//" in k:
        k = k.replace("//", "/")
    return k

# เมื่อ backend เป็น S3/MinIO จะใช้ s3util (ของโปรเจกต์)
if STORAGE_BACKEND == "local":
    def _local_path_from_key(object_key: str) -> Path:
        # ปรับให้รับคีย์ที่มี / นำหน้า แล้วค่อย validate
        object_key = _norm_object_key(object_key)

        # จำกัด namespace และกัน traversal
        if not object_key or ".." in object_key or "://" in object_key:
            raise HTTPException(status_code=400, detail="invalid object_key")

        # ✅ รองรับทั้ง catalog/, storage/, staging/
        if not (
            object_key.startswith("catalog/")
            or object_key.startswith("storage/")
            or object_key.startswith("staging/")
        ):
            raise HTTPException(
                status_code=400,
                detail="object_key must be under catalog/, storage/ or staging/",
            )

        base = Path(UPLOADS_DIR_ABS).resolve()
        p = (base / object_key).resolve()
        if not str(p).startswith(str(base) + os.sep):
            raise HTTPException(status_code=400, detail="invalid object_key path")
        return p
else:
    # s3/minio helpers
    from s3util import open_object_stream, head_object, get_object_range, stream_s3_body


# ---------------- Helpers ----------------
def _guess_ct(name: str) -> str:
    n = (name or "").lower()
    if n.endswith((".gcode", ".gco", ".gc")):
        return "text/x.gcode"
    if n.endswith(".stl"):
        return "model/stl"
    if n.endswith(".3mf"):
        return "application/vnd.ms-package.3dmanufacturing-3dmodel"
    if n.endswith(".obj"):
        return "text/plain"
    ct, _ = mimetypes.guess_type(n)
    return ct or "application/octet-stream"


def _parse_range(hdr: Optional[str], size: int) -> Optional[Tuple[int, int]]:
    """
    คืน (start, end) แบบ inclusive ถ้า parse ได้, ไม่งั้น None
    รองรับ "bytes=START-END"
      - START อาจว่าง (bytes=-N → N ไบต์สุดท้าย)
      - END   อาจว่าง (bytes=START- → จนจบไฟล์)
    """
    if not hdr:
        return None
    hdr = hdr.strip()
    if not hdr.lower().startswith("bytes="):
        return None
    try:
        spec = hdr.split("=", 1)[1]
        if "," in spec:
            # ไม่รองรับหลายช่วง
            return None
        start_s, end_s = (spec or "").split("-", 1)
        if start_s == "":
            # bytes=-N → N bytes from end
            n = int(end_s)
            if n <= 0:
                return None
            start = max(0, size - n)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else (size - 1)
            if start < 0 or end < start:
                return None
        end = min(end, size - 1)
        return (start, end)
    except Exception:
        return None


def _invalid_range_response(size: int) -> Response:
    return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})


def _disposition_header(disp_name: str) -> str:
    # Content-Disposition ที่รองรับ UTF-8 ตาม RFC 5987
    ascii_name = "".join(ch if 32 <= ord(ch) < 127 else "_" for ch in disp_name)
    utf8_name = quote(disp_name, safe="")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}'


# ---------------- Endpoints ----------------
@router.get("/raw")
def files_raw(
    request: Request,
    object_key: str = Query(..., description="object key ของไฟล์ (เช่น catalog/... หรือ staging/... หรือ storage/...)"),
    _user = Depends(get_user_from_header_or_query),  # ✅ ใช้ token ได้ทั้ง Header และ ?token=
):
    """
    ส่งเนื้อไฟล์ดิบสำหรับพรีวิว/ดาวน์โหลด
    - รองรับ Range 206 + ส่วนหัว Accept-Ranges/ETag/Content-Range
    - ทำงานได้ทั้งโหมด local และ s3/minio
    """
    object_key = _norm_object_key(object_key)
    if not object_key:
        raise HTTPException(status_code=400, detail="object_key is required")

    # -------- LOCAL BACKEND --------
    if STORAGE_BACKEND == "local":
        p = _local_path_from_key(object_key)
        if not p.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        file_size = p.stat().st_size
        ct = _guess_ct(p.name)

        # ETag (mtime-size) + If-None-Match
        st = p.stat()
        etag = f'W/"{int(st.st_mtime)}-{st.st_size}"'
        inm = request.headers.get("if-none-match")
        if inm and etag in inm:
            return Response(status_code=304, headers={"ETag": etag})

        # Range 206
        rng_hdr = request.headers.get("range")
        rng = _parse_range(rng_hdr, file_size)
        if rng:
            start, end = rng
            length = end - start + 1
            if length <= 0:
                return _invalid_range_response(file_size)

            def _iter():
                with open(p, "rb") as f:
                    f.seek(start)
                    remaining = length
                    chunk = 256 * 1024
                    while remaining > 0:
                        data = f.read(min(chunk, remaining))
                        if not data:
                            break
                        remaining -= len(data)
                        yield data

            headers = {
                "Content-Type": ct,
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "ETag": etag,
                "Cache-Control": "no-store",
            }
            return StreamingResponse(_iter(), status_code=206, headers=headers)

        if rng_hdr and not rng:
            return _invalid_range_response(file_size)

        # ทั้งไฟล์ (local ใช้ FileResponse ได้)
        return FileResponse(
            str(p),
            media_type=ct,
            headers={"Accept-Ranges": "bytes", "ETag": etag, "Cache-Control": "no-store"},
        )

    # -------- S3/MINIO BACKEND --------
    # HEAD ก่อนเพื่อหา size/ctype/etag
    try:
        h = head_object(object_key)
        size = int(h.get("ContentLength") or 0)
        ct = (h.get("ContentType") or None) or _guess_ct(object_key)
        etag = h.get("ETag")
    except Exception:
        raise HTTPException(status_code=404, detail="Object not found")

    # ETag / If-None-Match
    inm = request.headers.get("if-none-match")
    if inm and etag and str(etag) in inm:
        return Response(status_code=304, headers={"ETag": str(etag)})

    rng_hdr = request.headers.get("range")
    rng = _parse_range(rng_hdr, size)
    if rng:
        # ---- Partial content (206) → ใส่ Content-Length ของ “ช่วง” ได้ปลอดภัย ----
        start, end = rng
        data = get_object_range(object_key, start=start, length=end - start + 1)
        headers = {
            "Content-Type": ct,
            "Accept-Ranges": "bytes",
            "Content-Length": str(len(data)),
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Cache-Control": "no-store",
        }
        if etag:
            headers["ETag"] = str(etag)
        return Response(content=data, status_code=206, headers=headers)
    elif rng_hdr:
        # ส่งมาเป็น Range แต่ parse ไม่ได้ → 416
        return _invalid_range_response(size)

    # ---- ทั้งไฟล์ (stream) จาก S3: *ห้าม* ใส่ Content-Length เพื่อกัน mismatch ----
    try:
        iterator, _full_size, ct_full = open_object_stream(object_key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Object not found")

    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
    }
    if etag:
        headers["ETag"] = str(etag)

    # iterator จาก open_object_stream() ใช้ stream_s3_body ภายใน (ทน connection reset)
    return StreamingResponse(iterator, media_type=ct_full or ct, headers=headers)


@router.get("/head")
def files_head(
    object_key: str = Query(..., description="object key ของไฟล์"),
    _user = Depends(get_user_from_header_or_query),
):
    """
    คืนข้อมูลหัวไฟล์ (content_length / content_type / etag) — ใช้เช็คก่อนโหลดจริง
    """
    object_key = _norm_object_key(object_key)
    if not object_key:
        raise HTTPException(status_code=400, detail="object_key is required")

    if STORAGE_BACKEND == "local":
        p = _local_path_from_key(object_key)
        if not p.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        size = p.stat().st_size
        ct = _guess_ct(p.name)
        return {
            "content_length": size,
            "content_type": ct,
            "etag": None,
            "accept_ranges": "bytes",
        }

    # s3/minio
    try:
        h = head_object(object_key)
    except Exception:
        raise HTTPException(status_code=404, detail="Object not found")
    return {
        "content_length": h.get("ContentLength"),
        "content_type": h.get("ContentType"),
        "etag": h.get("ETag"),
        "accept_ranges": "bytes",
    }


@router.get("/download")
def files_download(
    request: Request,
    object_key: str = Query(..., description="object key ของไฟล์"),
    filename: Optional[str] = Query(None, description="ชื่อไฟล์ที่จะแนบใน Content-Disposition"),
    _user = Depends(get_user_from_header_or_query),
):
    """
    Force download (แนบ Content-Disposition: attachment; filename="..."; filename*=UTF-8'')
    รองรับ Range เช่นกัน (เผื่อ client ที่อยาก resume)
    """
    object_key = _norm_object_key(object_key)
    if not object_key:
        raise HTTPException(status_code=400, detail="object_key is required")

    # -------- LOCAL --------
    if STORAGE_BACKEND == "local":
        p = _local_path_from_key(object_key)
        if not p.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        ct = _guess_ct(p.name)
        disp_name = filename or p.name
        return FileResponse(
            str(p),
            media_type=ct,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Disposition": _disposition_header(disp_name),
                "Cache-Control": "private, max-age=0, no-cache",
            },
        )

    # -------- S3/MINIO --------
    try:
        h = head_object(object_key)
        size = int(h.get("ContentLength") or 0)
        ct = (h.get("ContentType") or None) or _guess_ct(object_key)
        etag = h.get("ETag")
    except Exception:
        raise HTTPException(status_code=404, detail="Object not found")

    disp_name = filename or Path(object_key).name
    disp = _disposition_header(disp_name)

    rng_hdr = request.headers.get("range")
    rng = _parse_range(rng_hdr, size)
    if rng:
        start, end = rng
        data = get_object_range(object_key, start=start, length=end - start + 1)
        headers = {
            "Content-Type": ct,
            "Accept-Ranges": "bytes",
            "Content-Length": str(len(data)),
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Disposition": disp,
            "Cache-Control": "private, max-age=0, no-cache",
        }
        if etag:
            headers["ETag"] = str(etag)
        return Response(content=data, status_code=206, headers=headers)
    elif rng_hdr:
        return _invalid_range_response(size)

    # ทั้งไฟล์ (stream) — **ไม่ใส่ Content-Length**
    try:
        iterator, _full_size, ct_full = open_object_stream(object_key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Object not found")

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": disp,
        "Cache-Control": "private, max-age=0, no-cache",
    }
    if etag:
        headers["ETag"] = str(etag)

    return StreamingResponse(iterator, media_type=ct_full or ct, headers=headers)


@router.get("/exists")
def files_exists(
    object_key: str = Query(..., description="object key ของไฟล์"),
    _user = Depends(get_user_from_header_or_query),
):
    """
    เช็คว่ามีไฟล์จริงไหม (เร็ว ๆ)
    """
    object_key = _norm_object_key(object_key)
    if not object_key:
        raise HTTPException(status_code=400, detail="object_key is required")

    if STORAGE_BACKEND == "local":
        p = _local_path_from_key(object_key)
        return {"exists": p.is_file()}

    try:
        _ = head_object(object_key)
        return {"exists": True}
    except Exception:
        return {"exists": False}