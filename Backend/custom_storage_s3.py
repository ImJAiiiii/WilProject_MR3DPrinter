# backend/custom_storage_s3.py
from __future__ import annotations

import os, re, json, tempfile, subprocess, base64, logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple, Dict

from fastapi import APIRouter, Depends, HTTPException, Query, Response, Body
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from pydantic import BaseModel

from db import get_db
from auth import get_current_user, get_confirmed_user, get_manager_user
from models import StorageFile, User

# ===== SCHEMAS: optional import, with local fallbacks =====
try:
    from schemas import (
        StorageUploadRequestIn, StorageUploadRequestOut,
        StorageUploadCompleteIn, StorageFileOut, StorageUploaderOut,
        StorageValidateNameIn, StorageValidateNameOut, StorageSearchNamesOut,
        FinalizeIn,
    )
except Exception:
    class StorageUploadRequestIn(BaseModel):
        filename: str | None = None
        size: int | None = None
        content_type: str | None = None

    class StorageUploadRequestOut(BaseModel):
        object_key: str
        method: str = "PUT"
        url: str
        headers: dict = {}
        expires_in: int = 600

    class StorageUploadCompleteIn(BaseModel):
        object_key: str
        filename: str | None = None
        size: int | None = None
        content_type: str | None = None
        auto_finalize: bool | None = None
        model: str | None = None
        target: str | None = None

    class StorageUploaderOut(BaseModel):
        employee_id: str | None = None
        name: str | None = None
        email: str | None = None

    class StorageFileOut(BaseModel):
        id: int | None = None
        filename: str | None = None
        name: str | None = None
        object_key: str
        content_type: str | None = None
        size: int | None = None
        uploaded_at: datetime | None = None
        url: str | None = None
        uploader: StorageUploaderOut | None = None

    class StorageValidateNameIn(BaseModel):
        name: str
        ext: str | None = "gcode"
        require_pattern: bool = False

    class StorageValidateNameOut(BaseModel):
        ok: bool
        reason: str | None = None
        normalized: str | None = None
        exists: bool | None = None
        suggestions: list[str] = []

    class StorageSearchNamesOut(BaseModel):
        items: list[str] = []

    class FinalizeIn(BaseModel):
        object_key: str
        filename: str
        content_type: str | None = None
        size: int | None = None
        model: str | None = "Default"
        target: str | None = "catalog"

from s3util import (
    presign_put, presign_get, head_object, delete_object, copy_object,
    get_object_range, list_objects, _guess_content_type as _s3_guess_ct,
    new_staging_key, new_storage_key_for_model, catalog_paths_for_job,
    staging_triple_keys, commit_triple_to_catalog, upload_bytes,
    download_to_file, normalize_s3_prefix,
)

# ---------- preview renderer (optional) ----------
_HAS_RENDERER = True
try:
    from preview_gcode_image import gcode_to_preview_png  # must exist in project
except Exception:
    gcode_to_preview_png = None  # type: ignore
    _HAS_RENDERER = False

# ---------- manifest (optional) ----------
logger = logging.getLogger("custom_storage_s3")

_HAS_MANIFEST = True
try:
    from gcode_manifest import (  # type: ignore
        write_manifest_for_gcode,
        presign_manifest_for_gcode,
        manifest_key_for,
    )
except Exception:
    write_manifest_for_gcode = None            # type: ignore
    presign_manifest_for_gcode = None          # type: ignore
    manifest_key_for = None                    # type: ignore
    _HAS_MANIFEST = False

# ---------- siblings / cleanup helpers ----------
def _preview_keys_from_gcode(gk: str | None) -> list[str]:
    """คืนรายชื่อ key preview ทั้งสองแบบ (.preview.png และ _preview.png)"""
    if not gk or not re.search(r"\.(gcode|gco|gc)$", gk, flags=re.I):
        return []
    base = re.sub(r"\.(gcode|gco|gc)$", "", gk, flags=re.I)
    return [f"{base}.preview.png", f"{base}_preview.png"]

def _manifest_key_for_safe(gk: str | None) -> str | None:
    if not gk:
        return None
    if _HAS_MANIFEST and manifest_key_for:
        try:
            mk = manifest_key_for(gk)  # type: ignore
            if mk:
                return mk
        except Exception:
            pass
    try:
        return str(Path(gk).with_suffix(".json")).replace("\\", "/")
    except Exception:
        return None

def _delete_siblings_all(db: Session, main_gcode_key: str):
    """ลบ preview/manifest บน S3 + ลบ row DB ของไฟล์พี่น้อง"""
    sibs: List[str] = []
    sibs += _preview_keys_from_gcode(main_gcode_key)
    mk = _manifest_key_for_safe(main_gcode_key)
    if mk:
        sibs.append(mk)

    # ลบ object บน S3
    for k in sibs:
        try:
            delete_object(k)
        except Exception:
            pass

    # ลบ row DB ที่ชี้ไปยัง key เหล่านี้
    if sibs:
        try:
            (db.query(StorageFile)
               .filter(StorageFile.object_key.in_(sibs))
               .delete(synchronize_session=False))
            db.commit()
        except Exception:
            db.rollback()

import boto3

# === ใช้ prefix ให้ตรงกับ FE ===
router = APIRouter(prefix="/api/storage", tags=["storage"])

# ---------- config / limits ----------
MAX_GCODE_MB = int(os.getenv("MAX_GCODE_MB", "256"))
MAX_GCODE_BYTES = MAX_GCODE_MB * 1024 * 1024

S3_ENDPOINT = os.getenv("S3_ENDPOINT")
S3_REGION = os.getenv("S3_REGION", "us-east-1")
S3_BUCKET = os.getenv("S3_BUCKET", "")
S3_SECURE = str(os.getenv("S3_SECURE", "false")).lower() in ("1", "true", "yes")
PRESIGN_EXPIRES = int(os.getenv("PRESIGN_EXPIRES", "600"))

# preview switches
SLICER_PREVIEW_SIZE = os.getenv("SLICER_PREVIEW_SIZE", "1200x900").strip()
SLICER_PREVIEW_HIDE_TRAVEL = str(os.getenv("SLICER_PREVIEW_HIDE_TRAVEL", "1")).lower() in ("1","true","yes")
SLICER_PREVIEW_DPI = int(os.getenv("SLICER_PREVIEW_DPI", "0") or "0")  # 0 = auto from size
SLICER_PREVIEW_LW = float(os.getenv("SLICER_PREVIEW_LW", "0.80"))
SLICER_PREVIEW_FADE = float(os.getenv("SLICER_PREVIEW_FADE", "0.72"))
SLICER_PREVIEW_AA = str(os.getenv("SLICER_PREVIEW_AA", "1")).lower() in ("1","true","yes")

AUTO_PREVIEW_ON_FINALIZE = str(os.getenv("AUTO_PREVIEW_ON_FINALIZE", "1")).lower() in ("1","true","yes")
AUTO_MANIFEST_ON_FINALIZE = str(os.getenv("AUTO_MANIFEST_ON_FINALIZE", "1")).lower() in ("1","true","yes")

# ---------- constants / regex ----------
NAME_REGEX = re.compile(r"^[A-Za-z0-9._-]+_V\d+$")
_HEX_PREFIX = re.compile(r"^[0-9a-f]{8,}[_-](.+)", re.I)
_V_RE = re.compile(r"^(?P<stem>.+?)_v(?P<n>\d+)(?:\.(?P<ext>[^.]+))?$", re.I)

# ---------- PrusaSlicer env ----------
PRUSA_SLICER_BIN = os.getenv("PRUSA_SLICER_BIN", "").strip()
PRUSA_DATADIR = os.getenv("PRUSA_DATADIR", "").strip()
PRUSA_BUNDLE_PATH = os.getenv("PRUSA_BUNDLE_PATH", "").strip()
PRUSA_PRINTER_PRESET = os.getenv("PRUSA_PRINTER_PRESET", "").strip()
PRUSA_PRINT_PRESET = os.getenv("PRUSA_PRINT_PRESET", "").strip()
PRUSA_FILAMENT_PRESET = os.getenv("PRUSA_FILAMENT_PRESET", "").strip()
PRUSA_STRICT_PRESET = str(os.getenv("PRUSA_STRICT_PRESET", "1")).lower() in ("1", "true", "yes")

# ---------------- helpers ----------------
def _strip_hash_prefix(filename: str) -> str:
    m = _HEX_PREFIX.match(filename or "")
    return m.group(1) if m else filename

def _human_size(n: int | None) -> str | None:
    if not n or n <= 0: return None
    units = ["B","KB","MB","GB","TB"]; s=float(n); i=0
    while s>=1024 and i<len(units)-1: s/=1024.0; i+=1
    return f"{s:.1f} {units[i]}" if i>=2 else f"{int(s)} {units[i]}"

def _name_low(s: str) -> str: return (s or "").strip().lower()

def _is_manager(u: User) -> bool:
    return bool(getattr(u,"is_manager",False) or getattr(u,"can_manage_queue",False) or (getattr(u,"role","") or "").lower()=="manager")

def _owner_or_manager(u: User, row: StorageFile) -> bool:
    return (row.employee_id or "")==(u.employee_id or "") or _is_manager(u)

def _uploader(u: Optional[User], fallback_emp: Optional[str]=None) -> StorageUploaderOut:
    emp=(u.employee_id if u else (fallback_emp or "")).strip()
    name=(u.name if (u and u.name) else emp) or None
    email=u.email if u else None
    return StorageUploaderOut(employee_id=emp or None, name=name, email=email)

def _validate_key(key: str) -> str:
    k=(key or "").strip()
    if not k or k.startswith("/") or ".." in k or "://" in k: raise HTTPException(400,"invalid object_key")
    if not (k.startswith("storage/") or k.startswith("staging/") or k.startswith("catalog/")):
        raise HTTPException(400,"object_key must be under catalog/ or storage/ or staging/")
    return k

def _is_gcode_name(name: str) -> bool:
    n=(name or "").lower()
    return n.endswith(".gcode") or n.endswith(".gco") or n.endswith(".gc")

def _is_stl_name(name: str) -> bool:
    return (name or "").lower().endswith(".stl")

def _guess_ct(name: str, default: str="application/octet-stream") -> str:
    return _s3_guess_ct(name) or default

def _to_out(db: Session, row: StorageFile) -> StorageFileOut:
    try: url = presign_get(row.object_key)
    except Exception: url = None
    u = db.query(User).filter(User.employee_id==row.employee_id).first()
    return StorageFileOut(
        id=row.id, filename=row.filename, name=getattr(row,"name",None), object_key=row.object_key,
        content_type=row.content_type, size=row.size, uploaded_at=row.uploaded_at,
        url=url, uploader=_uploader(u, fallback_emp=row.employee_id),
    )

def _ensure_ext(name: str, ext: str="gcode") -> str:
    n=(name or "").strip(); e=(ext or "").strip().lstrip(".").lower() or "gcode"
    if not n: return f"model_V1.{e}"
    if "." not in n: return f"{n}.{e}"
    return n

def _normalize_name_for_check(name: str, ext: str="gcode", require_pattern: bool=True) -> Tuple[str, Optional[str]]:
    nn=_ensure_ext(name, ext)
    if require_pattern:
        base=nn.rsplit(".",1)[0]
        if not NAME_REGEX.match(base): return nn,"invalid_format"
    return nn,None

def _exists_name_low(db: Session, name: str) -> bool:
    """ทน schema ต่างกัน: name_low / name / filename"""
    nl = _name_low(name)
    try:
        if hasattr(StorageFile, "name_low"):
            return db.query(StorageFile.id).filter(getattr(StorageFile, "name_low") == nl).first() is not None
        if hasattr(StorageFile, "name"):
            return db.query(StorageFile.id).filter(func.lower(getattr(StorageFile, "name")) == nl).first() is not None
        return db.query(StorageFile.id).filter(func.lower(StorageFile.filename) == nl).first() is not None
    except Exception:
        return False

def _split_version(fullname: str) -> tuple[str, Optional[int], Optional[str]]:
    name=(fullname or "").strip(); m=_V_RE.match(name)
    if not m:
        if "." in name: return name.rsplit(".",1)[0], None, name.rsplit(".",1)[-1]
        return name,None,None
    return m.group("stem") or "", int(m.group("n")) if m.group("n") else None, m.group("ext")

def _bump_until_free(db: Session, desired: str) -> str:
    desired=(desired or "").strip()
    desired=_ensure_ext(desired, ext=desired.rsplit(".",1)[-1] if "." in desired else "gcode")
    if not _exists_name_low(db, desired): return desired
    stem, n, ext = _split_version(desired)
    if ext is None and "." in desired: ext=desired.rsplit(".",1)[-1]
    start=(n+1) if n is not None else 2
    for i in range(start, start+500):
        cand=f"{stem}_V{i}.{ext or 'gcode'}"
        if not _exists_name_low(db, cand): return cand
    return desired

# ---------- S3 client ----------
def _s3_client():
    return boto3.client(
        "s3",
        region_name=S3_REGION or None,
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=os.getenv("S3_ACCESS_KEY"),
        aws_secret_access_key=os.getenv("S3_SECRET_KEY"),
        verify=S3_SECURE,
    )

def _download_tmp_via_boto(key: str, suffix: str) -> str:
    cli=_s3_client(); tmp=tempfile.NamedTemporaryFile(delete=False, suffix=suffix); tmp.close()
    try: cli.download_file(S3_BUCKET, key, tmp.name)
    except Exception as e:
        try: os.unlink(tmp.name)
        except Exception: pass
        raise HTTPException(404, f"Object not found: {key} ({e})")
    return tmp.name

def _download_gcode_to_tmp(key: str) -> str: return _download_tmp_via_boto(key, ".gcode")
def _download_stl_to_tmp(key: str) -> str:   return _download_tmp_via_boto(key, ".stl")

# ---------- Slice STL → local G-code ----------
def _slice_stl_to_gcode(stl_path: str) -> str:
    if not PRUSA_SLICER_BIN: raise HTTPException(500, "PRUSA_SLICER_BIN is not configured")
    out_path=tempfile.NamedTemporaryFile(delete=False, suffix=".gcode"); out_path.close()

    def build_cmd(use_presets: bool)->list[str]:
        cmd=[PRUSA_SLICER_BIN,"--export-gcode","--output",out_path.name]
        if PRUSA_DATADIR: cmd+=["--datadir",PRUSA_DATADIR]
        if PRUSA_BUNDLE_PATH: cmd+=["--load",PRUSA_BUNDLE_PATH]
        if use_presets:
            if PRUSA_PRINTER_PRESET:  cmd+=["--printer",PRUSA_PRINTER_PRESET]
            if PRUSA_PRINT_PRESET:    cmd+=["--print-settings",PRUSA_PRINT_PRESET]
            if PRUSA_FILAMENT_PRESET: cmd+=["--filament-settings",PRUSA_FILAMENT_PRESET]
            if PRUSA_STRICT_PRESET:   cmd+=["--strict-presets"]
        cmd.append(stl_path); return cmd

    def run_cmd(cmd: list[str]): subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    try:
        run_cmd(build_cmd(True)); return out_path.name
    except subprocess.CalledProcessError as e:
        err=(e.stderr or b"").decode(errors="ignore")
        unknown = any(s in err for s in ["Unknown option --printer","Unknown option --print-settings","Unknown option --filament-settings","unknown option --printer"])
        if unknown or not PRUSA_STRICT_PRESET:
            try: run_cmd(build_cmd(False)); return out_path.name
            except subprocess.CalledProcessError as e2:
                err2=(e2.stderr or b"").decode(errors="ignore")
                try: os.unlink(out_path.name)
                except Exception: pass
                raise HTTPException(500, f"PrusaSlicer failed after retry. err1={err.strip()[:500]} | err2={err2.strip()[:500]}")
        try: os.unlink(out_path.name)
        except Exception: pass
        raise HTTPException(500, f"PrusaSlicer failed: {err.strip()[:500]}")
    except Exception as e:
        try: os.unlink(out_path.name)
        except Exception: pass
        raise HTTPException(500, f"PrusaSlicer error: {e}")

# ---------- Preview helpers ----------
def _parse_size_to_dpi(size_str: str) -> int:
    if SLICER_PREVIEW_DPI > 0:
        return max(96, min(600, int(SLICER_PREVIEW_DPI)))
    try:
        w,h=(int(s) for s in str(size_str).lower().split("x",1))
        dpi=max(int(round(max(w/8.0,h/6.0))),96)
        return min(dpi,600)
    except Exception:
        return 150  # 8x6 * 150 = 1200x900

_GRAY1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAADUlEQVR42mNgYGBgBwABJQEZC2d1gQAAAABJRU5ErkJggg=="
)

def _placeholder_png() -> bytes:
    return _GRAY1

def _valid_png(data: bytes) -> bool:
    return bool(data and len(data) > 4096 and data[:8] == b"\x89PNG\r\n\x1a\n")

def _render_preview_from_local_gcode(local_gcode_path: str) -> bytes:
    if not _HAS_RENDERER or gcode_to_preview_png is None:
        return _placeholder_png()

    png_tmp=tempfile.NamedTemporaryFile(delete=False, suffix=".png"); png_tmp.close()
    dpi=_parse_size_to_dpi(SLICER_PREVIEW_SIZE)

    def _attempt(params: dict) -> Optional[bytes]:
        try:
            gcode_to_preview_png(local_gcode_path, png_tmp.name, **params)  # type: ignore
            with open(png_tmp.name,"rb") as f:
                data=f.read()
            if _valid_png(data):
                return data
            return None
        except Exception:
            return None

    try:
        a_params=dict(
            include_travel=(not SLICER_PREVIEW_HIDE_TRAVEL),
            lw=SLICER_PREVIEW_LW,
            fade=SLICER_PREVIEW_FADE,
            zscale=1.0,
            pad=0.08,
            grid=0.0,
            dpi=dpi,
            antialias=SLICER_PREVIEW_AA,
        )
        data=_attempt(a_params)
        if data: return data

        b_params=dict(
            include_travel=False,
            lw=max(0.55, SLICER_PREVIEW_LW),
            fade=min(0.85, max(0.65, SLICER_PREVIEW_FADE)),
            zscale=1.0,
            pad=0.12,
            grid=0.0,
            dpi=max(150, dpi),
            antialias=True,
        )
        data=_attempt(b_params)
        if data: return data

        return _placeholder_png()
    finally:
        try: os.unlink(png_tmp.name)
        except Exception: pass

# =====================================================================
# Catalog listing
# =====================================================================
@router.get("/catalog")
def list_catalog(
    model: Optional[str]=Query(None), q: Optional[str]=Query(None),
    offset: int=Query(0, ge=0), limit: int=Query(200, ge=1, le=2000),
    with_urls: bool=Query(False), with_head: bool=Query(False),
    db: Session=Depends(get_db), _me: User=Depends(get_current_user),
):
    def is_gcode_key(k: str)->bool:
        kl=(k or "").lower(); return kl.endswith(".gcode") or kl.endswith(".gco") or kl.endswith(".gc")
    def looks_like_preview(name: str)->bool:
        n=(name or "").lower(); return ("preview" in n) and n.endswith((".webp",".png",".jpg",".jpeg"))
    cat_prefix = normalize_s3_prefix(f"catalog/{model.title()}/") if model else "catalog/"
    objs=list_objects(Prefix=cat_prefix) or []
    groups: Dict[str, Dict]={}
    for o in objs:
        key=o.get("Key") or ""
        if not key.startswith("catalog/"): continue
        if q and q.lower() not in key.lower(): continue
        parts=key.split("/")
        if len(parts)<3: continue
        model_name=parts[1]
        piece_folder = (Path(parts[2]).stem if len(parts)==3 else parts[2])
        gid=f"catalog/{model_name}/{piece_folder}/"
        entry=groups.setdefault(gid,{"model":model_name,"piece":piece_folder,"gcode_key":None,"first_key":None,"size":None,"uploaded_at":None,"preview_key":None,"meta_key":None})
        if entry["first_key"] is None:
            entry["first_key"]=key; entry["size"]=o.get("Size"); entry["uploaded_at"]=o.get("LastModified")
        fname_low=parts[-1].lower()
        if is_gcode_key(key):
            entry["gcode_key"]=key
            entry["preview_key"]=entry["preview_key"] or str(Path(key).with_suffix(".preview.png")).replace("\\","/")
        elif looks_like_preview(fname_low):
            entry["preview_key"]=key
        elif fname_low.endswith(".json"):
            entry["meta_key"]=key

    items: List[Dict]=[]
    for e in groups.values():
        rep_key=e["gcode_key"] or e["first_key"]
        if not rep_key: continue
        display=e["piece"] or Path(rep_key).stem
        size=e["size"]; content_type=None
        head_key=e.get("gcode_key") or e.get("first_key")
        if (size is None or size==0 or with_head) and head_key:
            try:
                h=head_object(head_key)
                size=int(h.get("ContentLength",0) or 0) or size
                content_type=h.get("ContentType") or content_type
            except Exception: pass
        preview_url=None
        if with_urls and e.get("preview_key"):
            try:
                _ = head_object(e["preview_key"])
                preview_url=presign_get(e["preview_key"])
            except Exception:
                preview_url=None
        uploader=None
        lookup_key=e.get("gcode_key") or e.get("first_key")
        if lookup_key:
            try:
                row=db.query(StorageFile).filter(StorageFile.object_key==lookup_key).first()
                if row:
                    u=db.query(User).filter(User.employee_id==row.employee_id).first()
                    uploader={"employee_id":(u.employee_id if u else row.employee_id),
                              "name":(u.name if u and u.name else row.employee_id),
                              "email":(u.email if u else None)}
            except Exception: uploader=None
        ext=None; rep_last=rep_key.split("/")[-1]
        if "." in rep_last: ext=rep_last.rsplit(".",1)[-1].lower()
        manifest_url=None
        if with_urls and e.get("meta_key"):
            try: manifest_url=presign_get(e.get("meta_key"))
            except Exception: manifest_url=None
        items.append({
            "display_name":display,"name":display,"filename":display,
            "object_key":e.get("gcode_key") or e["first_key"], "model":(e["model"] or ""),
            "size":size, "size_text":_human_size(size), "content_type":content_type, "ext":ext,
            "uploaded_at":e["uploaded_at"], "thumb":e.get("preview_key"),
            "preview_url":preview_url, "json_key":e.get("meta_key"), "json_url":manifest_url,
            "uploader":uploader, "stats":None,
        })
    items.sort(key=lambda x: x.get("uploaded_at") or "", reverse=True)
    total=len(items); items=items[offset:offset+limit]
    return {
        "ok": True,
        "items": items,
        "count": len(items),
        "total": total,
        "offset": offset,
        "limit": limit,
    }

# ---------- claim owner ----------
class ClaimOwnerIn(BaseModel):
    object_key: str
    employee_id: Optional[str]=None

@router.post("/catalog/claim", response_model=StorageFileOut)
def claim_owner(payload: ClaimOwnerIn, db: Session=Depends(get_db), me: User=Depends(get_manager_user)):
    key=_validate_key(payload.object_key)
    if not key.startswith(("catalog/","storage/")): raise HTTPException(400,"Only catalog/ or storage/ keys can be claimed")
    row=db.query(StorageFile).filter(StorageFile.object_key==key).first()
    emp=(payload.employee_id or me.employee_id or "").strip()
    if not emp: raise HTTPException(400,"employee_id is required")
    size,ctype,etag=None,None,None
    try:
        h=head_object(key); size=int(h.get("ContentLength",0) or 0); ctype=h.get("ContentType") or _guess_ct(key); etag=h.get("ETag")
    except Exception: pass
    fname=os.path.basename(key)
    base_no_ext, ext=(fname.rsplit(".",1)[0], fname.rsplit(".",1)[-1]) if "." in fname else (fname,"gcode")
    final_name=_bump_until_free(db, f"{base_no_ext}.{ext}")
    if not row:
        row=StorageFile(employee_id=emp, filename=fname, name=final_name, object_key=key,
                        content_type=ctype, size=size, uploaded_at=datetime.now(timezone.utc))
        db.add(row)
    else:
        row.employee_id=emp or row.employee_id
        row.filename=row.filename or fname
        row.name=row.name or final_name
        row.content_type=row.content_type or ctype
        row.size=row.size or size
    db.commit(); db.refresh(row); return _to_out(db,row)

# ---------- upload (presigned PUT → staging/*) ----------
@router.post("/upload/request", response_model=StorageUploadRequestOut)
def request_upload(
    filename: Optional[str] = Query(None),
    size: Optional[int] = Query(None),
    content_type: Optional[str] = Query(None),
    overwrite: bool = Query(False),
    payload: Optional[StorageUploadRequestIn] = None,
    db: Session = Depends(get_db),
    me: User = Depends(get_confirmed_user),
):
    if payload is not None:
        filename = payload.filename or filename
        size = payload.size if payload.size is not None else size
        content_type = payload.content_type or content_type

    if not filename:
        raise HTTPException(status_code=422, detail="filename is required")

    size = int(size or 0)
    ct = (content_type or "").strip()

    lower = filename.strip().lower()
    if not (lower.endswith(".stl") or lower.endswith(".gcode")
            or lower.endswith(".gco") or lower.endswith(".gc")):
        raise HTTPException(status_code=422, detail="Only STL and G-code are allowed")

    if not ct:
        ct = _guess_ct(filename)
    if lower.endswith(".stl"):
        ct = "model/stl"
    elif _is_gcode_name(lower):
        ct = "text/x.gcode"

    key = new_staging_key(filename)
    signed = presign_put(key, ct, size)

    return StorageUploadRequestOut(
        object_key=signed["object_key"],
        method=signed.get("method", "PUT"),
        url=signed["url"],
        headers=signed.get("headers", {}),
        expires_in=int(signed.get("expires_in", 600)),
    )

# ---------- upload/complete ----------
@router.post("/upload/complete", response_model=StorageFileOut)
def complete_upload(payload: StorageUploadCompleteIn, db: Session=Depends(get_db), me: User=Depends(get_confirmed_user)):
    key=_validate_key(payload.object_key)
    name_for_check=payload.filename or os.path.basename(key)
    lower=name_for_check.lower()
    if not (_is_gcode_name(lower) or _is_stl_name(lower)):
        raise HTTPException(422,"Only STL and G-code are allowed in custom storage")
    if getattr(payload,"auto_finalize",False):
        fin=FinalizeIn(object_key=key, filename=name_for_check, content_type=payload.content_type,
                       size=payload.size, model=(getattr(payload,"model",None) or "Delta"),
                       target=(getattr(payload,"target","catalog") or "catalog"))
        return finalize_to_storage(fin, db=db, me=me)  # type: ignore
    normalized,_=_normalize_name_for_check(name_for_check, ext=name_for_check.rsplit(".",1)[-1], require_pattern=False)
    final_name=_bump_until_free(db, normalized)
    size=payload.size; content_type=payload.content_type or _guess_ct(name_for_check)
    try:
        meta=head_object(key); size=size or int(meta.get("ContentLength",0) or 0); content_type=meta.get("ContentType") or content_type
    except Exception: pass
    if _is_gcode_name(lower) and size and size>MAX_GCODE_BYTES: raise HTTPException(413,f"File too large (>{MAX_GCODE_MB}MB)")
    row=StorageFile(employee_id=me.employee_id, filename=name_for_check, name=final_name, object_key=key,
                    content_type=content_type, size=size, uploaded_at=datetime.now(timezone.utc))
    db.add(row); db.commit(); db.refresh(row); return _to_out(db,row)

# ---------- finalize (staging → catalog/*) ----------
@router.post("/finalize", response_model=StorageFileOut)
def finalize_to_storage(payload: FinalizeIn, db: Session=Depends(get_db), me: Optional[User]=Depends(get_confirmed_user)):
    src_key=_validate_key(payload.object_key)
    if not src_key.startswith("staging/"): raise HTTPException(400,"object_key must be under staging/")
    is_src_gcode=_is_gcode_name(payload.filename); is_src_stl=_is_stl_name(payload.filename)
    if not (is_src_gcode or is_src_stl): raise HTTPException(422,"Only STL or G-code files can be finalized")
    model=(payload.model or "Default").strip()
    stem=(payload.filename.rsplit("/",1)[-1]).rsplit(".",1)[0]; stem=_strip_hash_prefix(stem); job_name=stem
    paths=catalog_paths_for_job(model, job_name)

    gcode_bytes=b""; local_gcode_path=None
    preview_bytes=None
    try:
        if is_src_gcode:
            local_gcode_path=_download_gcode_to_tmp(src_key)
        else:
            stl_tmp=_download_stl_to_tmp(src_key)
            try: local_gcode_path=_slice_stl_to_gcode(stl_tmp)
            finally:
                try: os.unlink(stl_tmp)
                except Exception: pass

        fsz=os.path.getsize(local_gcode_path)
        if fsz>MAX_GCODE_BYTES: raise HTTPException(413,f"File too large (>{MAX_GCODE_MB}MB)")
        with open(local_gcode_path,"rb") as fg: gcode_bytes=fg.read()

        if AUTO_PREVIEW_ON_FINALIZE:
            preview_bytes=_render_preview_from_local_gcode(local_gcode_path)
        else:
            preview_bytes=_placeholder_png()

        manifest_obj=None
        if AUTO_MANIFEST_ON_FINALIZE:
            if _HAS_MANIFEST:
                manifest_obj={"manifest_version":1,"name":job_name,"gcode_key":paths["gcode"],"preview_key":paths["preview"],
                              "summary":{},"applied":{},"source":{"original_key":src_key,"origin_ext":"gcode" if is_src_gcode else "stl"},
                              "slicer":{"presets":{}, "engine":"PrusaSlicer" if is_src_stl else "Unknown"},
                              "generated_at":datetime.utcnow().isoformat()+"Z","model":model}
            else:
                manifest_obj={"gcode_key":paths["gcode"],"name":job_name,"model":model,"preview_key":paths["preview"],
                              "generated_at":datetime.utcnow().isoformat()+"Z"}
        else:
            manifest_obj={"gcode_key":paths["gcode"]}

        manifest_bytes=json.dumps(manifest_obj, ensure_ascii=False, separators=(",",":")).encode("utf-8")

        tmp=staging_triple_keys(model, job_name)
        upload_bytes(gcode_bytes, tmp["gcode_tmp"], content_type="text/x.gcode")
        upload_bytes(manifest_bytes, tmp["json_tmp"], content_type="application/json; charset=utf-8")
        upload_bytes(preview_bytes or _placeholder_png(), tmp["preview_tmp"], content_type="image/png")
        final=commit_triple_to_catalog(model, job_name, tmp)

        if AUTO_MANIFEST_ON_FINALIZE and _HAS_MANIFEST and write_manifest_for_gcode:
            try:
                write_manifest_for_gcode(
                    gcode_key=final["gcode"],
                    job_name=job_name,
                    model=model,
                    info={},
                    applied={"presets": {}},
                    preview={"key": final.get("preview")} if final.get("preview") else None,
                    extra={"material": None},
                )
            except Exception:
                logger.exception("write_manifest_for_gcode failed (ignored)")
    finally:
        if local_gcode_path:
            try: os.unlink(local_gcode_path)
            except Exception: pass
    try: delete_object(src_key)
    except Exception: pass

    emp_id=getattr(me,"employee_id",None) or "unknown"
    out_filename=os.path.basename(paths["gcode"])
    final_name=_bump_until_free(db, f"{job_name}.gcode")
    row=StorageFile(employee_id=emp_id, filename=out_filename, name=final_name,
                    object_key=final["gcode"], content_type="text/x.gcode",
                    size=len(gcode_bytes), uploaded_at=datetime.now(timezone.utc))
    db.add(row); db.commit(); db.refresh(row); return _to_out(db,row)

# ---------- list (mine/by/all) ----------
def _apply_gcode_only(q):
    return q.filter(
        (StorageFile.filename.ilike("%.gcode")) | (StorageFile.filename.ilike("%.gco")) | (StorageFile.filename.ilike("%.gc")) |
        (StorageFile.object_key.ilike("%.gcode")) | (StorageFile.object_key.ilike("%.gco")) | (StorageFile.object_key.ilike("%.gc"))
    )

def _apply_model_filter(q, model: Optional[str]):
    if not model: return q
    return q.filter(or_(StorageFile.object_key.ilike(f"catalog/{model.title()}/%"),
                        StorageFile.object_key.ilike(f"storage/{model.lower()}/%")))

def _apply_namespace(q, include_staging: bool):
    if include_staging: return q
    return q.filter(or_(StorageFile.object_key.ilike("catalog/%"), StorageFile.object_key.ilike("storage/%")))

@router.get("/my", response_model=List[StorageFileOut])
def list_my_files(
    limit: int=Query(50, ge=1, le=200), model: Optional[str]=Query(None),
    q: Optional[str]=Query(None), include_staging: bool=Query(False),
    db: Session=Depends(get_db), me: User=Depends(get_current_user),
):
    rows_q=db.query(StorageFile).filter(StorageFile.employee_id==me.employee_id)
    rows_q=_apply_gcode_only(rows_q); rows_q=_apply_namespace(rows_q, include_staging); rows_q=_apply_model_filter(rows_q, model)
    if q: rows_q=rows_q.filter((StorageFile.filename.ilike(f"%{q}%")) | (StorageFile.name.ilike(f"%{q}%")))
    rows=rows_q.order_by(StorageFile.uploaded_at.desc(),StorageFile.id.desc()).limit(limit).all()
    return [_to_out(db,r) for r in rows]

@router.get("/by-user/{employee_id}", response_model=List[StorageFileOut])
def list_by_user(
    employee_id: str, limit: int=Query(50, ge=1, le=200), model: Optional[str]=Query(None),
    q: Optional[str]=Query(None), include_staging: bool=Query(False),
    db: Session=Depends(get_db), _manager: User=Depends(get_manager_user),
):
    emp=str(employee_id).strip()
    rows_q=db.query(StorageFile).filter(StorageFile.employee_id==emp)
    rows_q=_apply_gcode_only(rows_q); rows_q=_apply_namespace(rows_q, include_staging); rows_q=_apply_model_filter(rows_q, model)
    if q: rows_q=rows_q.filter((StorageFile.filename.ilike(f"%{q}%")) | (StorageFile.name.ilike(f"%{q}%")))
    rows=rows_q.order_by(StorageFile.uploaded_at.desc(),StorageFile.id.desc()).limit(limit).all()
    return [_to_out(db,r) for r in rows]

@router.get("", response_model=List[StorageFileOut])
def list_files(
    limit: int=Query(50, ge=1, le=200), model: Optional[str]=Query(None),
    q: Optional[str]=Query(None), include_staging: bool=Query(False),
    db: Session=Depends(get_db), _manager: User=Depends(get_manager_user),
):
    rows_q=db.query(StorageFile)
    rows_q=_apply_gcode_only(rows_q); rows_q=_apply_namespace(rows_q, include_staging); rows_q=_apply_model_filter(rows_q, model)
    if q: rows_q=rows_q.filter((StorageFile.filename.ilike(f"%{q}%")) | (StorageFile.name.ilike(f"%{q}%")))
    rows=rows_q.order_by(StorageFile.uploaded_at.desc(),StorageFile.id.desc()).limit(limit).all()
    return [_to_out(db,r) for r in rows]

# ---------- get / delete ----------
@router.get("/id/{fid}", response_model=StorageFileOut)
def get_file(fid: int, db: Session=Depends(get_db), me: User=Depends(get_current_user)):
    row=db.query(StorageFile).filter(StorageFile.id==fid).first()
    if not row or not _is_gcode_name(row.filename or row.object_key or ""): raise HTTPException(404,"Not found")
    if not _owner_or_manager(me,row): raise HTTPException(403,"forbidden")
    return _to_out(db,row)

@router.delete("/id/{fid}", status_code=204)
def delete_file(fid: int, db: Session=Depends(get_db), me: User=Depends(get_current_user),
                delete_object_from_s3: bool=Query(True)):
    row=db.query(StorageFile).filter(StorageFile.id==fid).first()
    if not row: return Response(status_code=204)
    _validate_key(row.object_key)
    if not _is_gcode_name(row.filename or row.object_key or ""): raise HTTPException(422,"Only G-code files can be deleted here")
    if not _owner_or_manager(me,row): raise HTTPException(403,"Only owner or manager can delete")

    if delete_object_from_s3:
        try: delete_object(row.object_key)
        except Exception: pass

    # ลบไฟล์พี่น้อง (preview / manifest) + row DB ของพี่น้อง
    _delete_siblings_all(db, row.object_key)

    try:
        db.delete(row)
        db.commit()
    except Exception:
        db.rollback()

    return Response(status_code=204)

@router.delete("/by-key", status_code=204)
def delete_by_key(object_key: str=Query(...), db: Session=Depends(get_db),
                  me: User=Depends(get_current_user), delete_object_from_s3: bool=Query(True)):
    key=_validate_key(object_key)
    row=db.query(StorageFile).filter(StorageFile.object_key==key).first()

    # ถ้าไม่มี row ต้องเป็นผู้จัดการ และลบได้เฉพาะ G-code เท่านั้น (เพราะตรวจ owner ไม่ได้)
    if not row:
        if not _is_manager(me):
            raise HTTPException(403, "Only manager can delete orphan objects")
        if not _is_gcode_name(key):
            raise HTTPException(422, "Only G-code files can be deleted here")

        if delete_object_from_s3:
            try:
                delete_object(key)
            except Exception:
                pass
        _delete_siblings_all(db, key)
        return Response(status_code=204)

    if not _is_gcode_name(row.filename or row.object_key or ""): raise HTTPException(422,"Only G-code files can be deleted here")
    if not _owner_or_manager(me,row): raise HTTPException(403,"Only owner or manager can delete")

    if delete_object_from_s3:
        try: delete_object(row.object_key)
        except Exception: pass

    _delete_siblings_all(db, row.object_key)

    try:
        db.delete(row)
        db.commit()
    except Exception:
        db.rollback()

    return Response(status_code=204)

@router.delete("/my")
def delete_my_files_bulk(
    older_than_days: Optional[int]=Query(None, ge=1),
    db: Session=Depends(get_db), me: User=Depends(get_current_user),
    delete_object_from_s3: bool=Query(True),
):
    q=db.query(StorageFile).filter(StorageFile.employee_id==me.employee_id)
    q=_apply_gcode_only(q)
    if older_than_days is not None:
        cutoff=datetime.now(timezone.utc)-timedelta(days=older_than_days)
        q=q.filter(StorageFile.uploaded_at < cutoff)
    rows=q.all(); count=0
    for r in rows:
        if delete_object_from_s3:
            try: delete_object(r.object_key)
            except Exception: pass
        _delete_siblings_all(db, r.object_key)
        try:
            db.delete(r); count+=1
        except Exception:
            db.rollback()
    db.commit(); return {"ok":True,"deleted":count}

# ---------- presign / head / range ----------
@router.get("/presign")
def presign_download(object_key: str=Query(...), with_meta: bool=Query(False), _me: User=Depends(get_current_user)):
    key=_validate_key(object_key)
    try: url=presign_get(key)
    except Exception as e: raise HTTPException(500,f"Failed to generate presigned url: {e}")
    if not with_meta: return {"url":url}
    meta={}
    try:
        h=head_object(key)
        meta={"content_type":h.get("ContentType"), "size":int(h.get("ContentLength",0) or 0),
              "etag":h.get("ETag"), "last_modified":h.get("LastModified").isoformat() if h.get("LastModified") else None}
    except Exception: pass
    return {"url":url, "meta":meta}

@router.get("/head")
def head(object_key: str=Query(...), _me: User=Depends(get_current_user)):
    key=_validate_key(object_key)
    try:
        h=head_object(key)
        return {"object_key":key, "content_type":h.get("ContentType"),
                "size":int(h.get("ContentLength",0) or 0), "etag":h.get("ETag"),
                "last_modified":h.get("LastModified").isoformat() if h.get("LastModified") else None}
    except Exception as e:
        raise HTTPException(404, f"Object not found: {e}")

@router.get("/range")
def get_range(
    object_key: str=Query(...), start: int=Query(0),
    length: int=Query(4_000_000, ge=1, le=10_000_000),
    _me: User=Depends(get_current_user),
):
    key=_validate_key(object_key)
    real_start=start; real_len=int(length)
    if start<0:
        try:
            meta=head_object(key); size=int(meta.get("ContentLength",0) or 0)
        except Exception as e: raise HTTPException(404,f"Head object failed: {e}")
        real_start=max(0, size+start); real_len=min(real_len, max(0, size-real_start))
    if real_len<=0: raise HTTPException(416,"Requested range not satisfiable")
    try:
        chunk=get_object_range(key, start=int(real_start), length=int(real_len))
        meta=head_object(key); size=int(meta.get("ContentLength",0) or 0)
    except Exception as e:
        raise HTTPException(404,f"Object range fetch failed: {e}")
    end=real_start+len(chunk)-1
    headers={"Cache-Control":"no-store","Accept-Ranges":"bytes","Content-Range":f"bytes {real_start}-{end}/{size}" if size else ""}
    resp=StreamingResponse(iter([chunk]), media_type="text/plain; charset=utf-8", headers=headers); resp.status_code=206
    return resp

# ---------- name utilities ----------
@router.post("/validate-name", response_model=StorageValidateNameOut)
def validate_name(payload: StorageValidateNameIn, db: Session=Depends(get_db), _me: User=Depends(get_current_user)):
    normalized, reason=_normalize_name_for_check(payload.name, ext=payload.ext or "gcode", require_pattern=payload.require_pattern)
    base=normalized.rsplit(".",1)[0]; ext=normalized.rsplit(".",1)[-1].lower()
    if reason=="invalid_format":
        return StorageValidateNameOut(ok=False, reason="invalid_format", normalized=normalized, exists=False, suggestions=[])
    exists=_exists_name_low(db, normalized)
    if exists:
        def _suggest_versions(base_no_ext: str, ext: str, db: Session, limit: int=5)->List[str]:
            suggestions=[]; m=re.search(r"_v(\d+)$", base_no_ext, re.I)
            start=int(m.group(1))+1 if m else 2; stem=re.sub(r"_v\d+$","",base_no_ext, flags=re.I) or base_no_ext
            i=start
            while len(suggestions)<limit and i<start+50:
                cand=f"{stem}_V{i}.{ext}"
                if not _exists_name_low(db, cand): suggestions.append(cand)
                i+=1
            return suggestions
        return StorageValidateNameOut(ok=False, reason="duplicate", normalized=normalized, exists=True,
                                      suggestions=_suggest_versions(base, ext, db, limit=5))
    return StorageValidateNameOut(ok=True, reason=None, normalized=normalized, exists=False, suggestions=[])

@router.get("/search-names", response_model=StorageSearchNamesOut)
def search_names(q: str=Query("", description="keyword (case-insensitive)"), limit: int=Query(8, ge=1, le=50),
                 db: Session=Depends(get_db), _me: User=Depends(get_current_user)):
    qq=(q or "").strip().lower()
    if not qq: return StorageSearchNamesOut(items=[])
    rows=(db.query(StorageFile.name).filter(func.lower(StorageFile.name).like(f"%{qq}%"))
          .order_by(StorageFile.name.asc()).limit(limit).all())
    return StorageSearchNamesOut(items=[r[0] for r in rows if r and r[0]])

# ---------- regenerate preview / manifest ----------
@router.post("/preview/regenerate")
def regenerate_preview(object_key: str=Query(...), _me: User=Depends(get_current_user), _db: Session=Depends(get_db)):
    key=_validate_key(object_key)
    if not _is_gcode_name(key): raise HTTPException(400,"object_key must be a G-code file (.gcode/.gco/.gc)")
    local_g=_download_gcode_to_tmp(key)
    try:
        png_bytes=_render_preview_from_local_gcode(local_g)
        preview_key=str(Path(key).with_suffix(".preview.png")).replace("\\","/")
        upload_bytes(png_bytes, preview_key, content_type="image/png")
        try: url=presign_get(preview_key)
        except Exception: url=""
        return {"ok":True,"preview_key":preview_key,"preview_url":url}
    finally:
        try: os.unlink(local_g)
        except Exception: pass

@router.post("/manifest/regenerate")
def regenerate_manifest(object_key: str=Query(...), _me: User=Depends(get_current_user), _db: Session=Depends(get_db)):
    key=_validate_key(object_key)
    if not _is_gcode_name(key): raise HTTPException(400,"object_key must be a G-code file (.gcode/.gco/.gc)")
    if not _HAS_MANIFEST or not write_manifest_for_gcode:
        desired=str(Path(key).with_suffix(".json")).replace("\\","/")
        upload_bytes(json.dumps({"gcode_key":key}, ensure_ascii=False).encode("utf-8"),
                     desired, content_type="application/json; charset=utf-8")
        try: url=presign_get(desired)
        except Exception: url=None
        return {"ok":True,"manifest_key":desired,"manifest_url":url}
    try:
        write_manifest_for_gcode(gcode_key=key, job_name=None, model=None, info=None, applied=None, preview=None, extra=None)
        try: old = manifest_key_for(key) if manifest_key_for else None
        except Exception: old=None
        desired=str(Path(key).with_suffix(".json")).replace("\\","/")
        if old!=desired:
            if old:
                try: copy_object(old, desired, content_type="application/json; charset=utf-8"); delete_object(old)
                except Exception:
                    upload_bytes(json.dumps({"gcode_key":key}, ensure_ascii=False).encode("utf-8"),
                                 desired, content_type="application/json; charset=utf-8")
            else:
                upload_bytes(json.dumps({"gcode_key":key}, ensure_ascii=False).encode("utf-8"),
                             desired, content_type="application/json; charset=utf-8")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Manifest failed: {e}")
    try:
        url = presign_manifest_for_gcode(key) if presign_manifest_for_gcode else presign_get(str(Path(key).with_suffix(".json")).replace("\\","/"))
    except Exception:
        url=None
    return {"ok":True,"manifest_key":str(Path(key).with_suffix(".json")).replace("\\","/"),"manifest_url":url}
