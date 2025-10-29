# backend/print_history.py
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Query, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from sqlalchemy import func

from db import get_db
from auth import get_current_user
from models import User, PrintJob
from schemas import PrintJobOut, PrintJobFileMeta

# ใช้ helper เดิมเพื่อ ensure แถวใน storage_files จาก object_key (idempotent)
from print_queue import _ensure_storage_record as ensure_storage_record

router = APIRouter(prefix="/history", tags=["history"])
log = logging.getLogger("history")


# -------------------------------------------------------------------
# Utilities
# -------------------------------------------------------------------

def _emp(x: object) -> str:
    return (str(x or "")).strip()


def _json_like_to_dict(val: Any) -> Optional[dict]:
    if val is None:
        return None
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            import json
            return json.loads(val)
        except Exception:
            return None
    try:
        return dict(val)
    except Exception:
        return None


def _merge_template_with_settings(template: Optional[dict], settings: Optional[dict]) -> Optional[dict]:
    if not template and not settings:
        return template or settings
    base = dict(template or {})
    if settings:
        for k, v in settings.items():
            if (k not in base) or (base[k] in (None, "", 0, False)):
                base[k] = v
    return base


def _is_gcode_key(k: Optional[str]) -> bool:
    if not k:
        return False
    kl = k.lower()
    return kl.endswith((".gcode", ".gco", ".gc"))


def _folder_name_from_key(key: str) -> Optional[str]:
    """
    ดึงชื่อโฟลเดอร์สุดท้ายจาก object_key เช่น
    catalog/Hontech/HT_CamMount_V1/Camera Mounting_oriented.gcode -> HT_CamMount_V1
    """
    try:
        d = os.path.dirname(key).strip("/\\")
        if not d:
            return None
        return d.split("/")[-1] or None
    except Exception:
        return None


def _preview_candidates(dir_: str, root: str) -> List[str]:
    """
    สร้างลิสต์ชื่อไฟล์ preview ที่เป็นไปได้:
    - {root}.preview.png / {root}_preview.png
    - รองรับ .png/.jpg/.jpeg
    - รองรับเคสมี/ไม่มี _oriented
    """
    root_no_oriented = root.replace("_oriented", "")
    bases = [
        root,                       # Camera Mounting_oriented
        root_no_oriented,           # Camera Mounting
        f"{root_no_oriented}_oriented",  # Camera Mounting_oriented (เผื่อกรณีตั้งชื่อแตกต่าง)
    ]
    exts = ["png", "jpg", "jpeg"]
    patterns = [
        "{b}.preview.{e}",
        "{b}_preview.{e}",
        "{b}_thumb.{e}",
        # เผื่อบางที่ใช้รูปแบบมีจุดและขีดล่างผสม
        "{b}_oriented.preview.{e}",
        "{b}_oriented_preview.{e}",
    ]

    out: List[str] = []
    for b in bases:
        for e in exts:
            for p in patterns:
                out.append(f"{dir_}/{p.format(b=b, e=e)}" if dir_ else p.format(b=b, e=e))
    # unique order
    seen, uniq = set(), []
    for k in out:
        if k not in seen:
            uniq.append(k)
            seen.add(k)
    return uniq


def _ensure_out_file(out: PrintJobOut) -> None:
    if out.file is None:
        out.file = PrintJobFileMeta()


def _set_thumb_if_found(out: PrintJobOut, key: str) -> None:
    """
    เซ็ตทั้ง out.file.thumb และ out.thumb (ให้ FE ใช้ได้แน่นอน)
    """
    _ensure_out_file(out)
    try:
        if not getattr(out.file, "thumb", None):
            out.file.thumb = key
        if not getattr(out, "thumb", None):
            setattr(out, "thumb", key)
    except Exception:
        pass


def _job_to_out(db: Session, current_emp: str, job: PrintJob) -> PrintJobOut:
    """
    แปลง ORM → PrintJobOut พร้อมพยายามเติมฟิลด์ json (template/stats/file)
    - รวม template.settings / settings_json เข้า template แบบ flatten
    - คำนวณ remaining_min แบบปลอดภัย
    - เติม name/preview/time_min
    - ชื่อ (display name) จะอ้างอิงชื่อโฟลเดอร์ใน MinIO เสมอ หากมี gcode_key
    """
    out = PrintJobOut.model_validate(job, from_attributes=True)

    # employee_name
    if not getattr(out, "employee_name", None):
        try:
            u = db.query(User).filter(User.employee_id == _emp(job.employee_id)).first()
            out.employee_name = (u.name if u and u.name else _emp(job.employee_id))
        except Exception:
            out.employee_name = _emp(job.employee_id)

    # map json columns
    if out.template is None:
        out.template = (
            _json_like_to_dict(getattr(job, "template_json", None))
            or _json_like_to_dict(getattr(job, "template", None))
        )
    if out.stats is None:
        out.stats = (
            _json_like_to_dict(getattr(job, "stats_json", None))
            or _json_like_to_dict(getattr(job, "stats", None))
        )
    if out.file is None:
        out.file = (
            _json_like_to_dict(getattr(job, "file_json", None))
            or _json_like_to_dict(getattr(job, "file", None))
        )

    # รวม settings เข้า template
    try:
        settings_col = (
            _json_like_to_dict(getattr(job, "settings_json", None))
            or _json_like_to_dict(getattr(job, "settings", None))
        )
        template_settings_nested = None
        if isinstance(out.template, dict):
            template_settings_nested = _json_like_to_dict(out.template.get("settings"))

        merged_template = _merge_template_with_settings(out.template, settings_col)
        merged_template = _merge_template_with_settings(merged_template, template_settings_nested)
        out.template = merged_template
    except Exception:
        pass

    # time_min จาก stats ถ้าหลักยังว่าง
    try:
        if getattr(out, "time_min", None) is None:
            if isinstance(out.stats, dict):
                tm = out.stats.get("time_min") or out.stats.get("timeMin")
                if tm is not None:
                    out.time_min = int(tm)
            elif out.stats and hasattr(out.stats, "time_min") and out.stats.time_min is not None:
                out.time_min = int(out.stats.time_min)
    except Exception:
        pass

    # keys
    raw_gkey = getattr(job, "gcode_key", None) or getattr(job, "gcode_path", None)
    gkey = _normalize_brand_case(raw_gkey) if raw_gkey else None
    key = gkey or ""

    # ------------------------------
    # ชื่อแสดงผล (Display Name)
    # ------------------------------
    try:
        name_is_missing_or_bad = (not out.name) or (isinstance(out.name, str) and out.name.lower().endswith(".stl"))

        # ใช้ชื่อโฟลเดอร์ใน MinIO เสมอ หากมี gcode_key อยู่ใต้ catalog/* หรือ storage/*
        if gkey and (gkey.startswith("catalog/") or gkey.startswith("storage/")):
            folder = _folder_name_from_key(gkey)
            if folder:
                out.name = folder
                name_is_missing_or_bad = False

        # ถ้ายังไม่มีชื่อ และเป็น gcode → ใช้ชื่อไฟล์ (ตัด _oriented/_preview/_thumb)
        if name_is_missing_or_bad and _is_gcode_key(key):
            base = os.path.basename(key)
            root, _ = os.path.splitext(base)
            for sfx in ("_oriented", "_preview", "_thumb"):
                root = root.replace(sfx, "")
            out.name = root or base
    except Exception:
        pass

    # ------------------------------
    # Preview (thumb): ตรวจเช็คไฟล์จริงใน S3
    # ------------------------------
    try:
        from s3util import head_object

        _ensure_out_file(out)

        # ถ้ายังไม่มีค่าอยู่แล้ว ค่อยไล่หา
        existing = getattr(out.file, "thumb", None) or getattr(out.file, "preview_key", None)
        if not existing:
            candidates: List[str] = []

            # 1) เดาจาก gcode_key (แม่นสุด)
            if _is_gcode_key(gkey):
                dir_, base = os.path.split(gkey)
                root, _ = os.path.splitext(base)
                candidates = _preview_candidates(dir_, root)

            # 2) ถ้ายังไม่มี หรือ gcode_key ไม่มี → เดาจากชื่อชิ้นงาน
            if not candidates:
                candidates = _preview_candidates_from_name(out.name)

            found = None
            for cand in candidates:
                try:
                    head_object(cand)  # 200 OK -> มีไฟล์
                    found = cand
                    break
                except Exception:
                    continue

            if found:
                # เซ็ตให้ FE อ่านได้ทั้งสองแบบ
                if not getattr(out.file, "thumb", None):
                    out.file.thumb = found
                # บาง FE ใช้ชื่อ preview_key
                if not getattr(out.file, "preview_key", None):
                    setattr(out.file, "preview_key", found)
    except Exception:
        # อย่าทำให้ล่มเพราะหา preview ไม่เจอ
        pass


    # remaining_min
    try:
        if job.status == "processing" and job.started_at and (job.time_min or 0) > 0:
            elapsed = max(0, int((datetime.utcnow() - job.started_at).total_seconds() // 60))
            out.remaining_min = max((job.time_min or 0) - elapsed, 0)
        elif job.status in {"queued", "paused"}:
            out.remaining_min = job.time_min or 0
    except Exception:
        pass

    # cancel permission
    out.me_can_cancel = bool((job.employee_id == current_emp) and (job.status in ("queued", "paused")))

    return out

def _normalize_brand_case(key: Optional[str]) -> Optional[str]:
    if not key:
        return key
    k = key.lstrip("/")
    k = k.replace("catalog/HONTECH/", "catalog/Hontech/")
    k = k.replace("catalog/DELTA/",   "catalog/Delta/")
    k = k.replace("storage/HONTECH/", "storage/Hontech/")
    k = k.replace("storage/DELTA/",   "storage/Delta/")
    return k

def _preview_candidates_from_name(name: Optional[str]) -> List[str]:
    if not name:
        return []
    brands = ["Hontech", "Delta"]
    bases = [name, name.replace("_oriented", ""), name.replace("_oriented", "") + "_oriented"]
    exts  = ["png", "jpg", "jpeg"]
    patterns = [
        "{b}.preview.{e}",
        "{b}_preview.{e}",
        "{b}_oriented_preview.{e}",
        "{b}_thumb.{e}",
    ]
    out: List[str] = []
    for br in brands:
        dir_ = f"catalog/{br}/{name}"
        for b in bases:
            for e in exts:
                for p in patterns:
                    out.append(f"{dir_}/{p.format(b=b, e=e)}")
    # unique
    seen, uniq = set(), []
    for k in out:
        if k not in seen:
            uniq.append(k); seen.add(k)
    return uniq

# -------------------------------------------------------------------
# GET /history/my
# -------------------------------------------------------------------

@router.get("/my", response_model=List[PrintJobOut])
def list_my_history(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    limit: int = Query(200, ge=1, le=1000),
    q: Optional[str] = Query(None, description="ค้นหาชื่อไฟล์/งาน (case-insensitive)"),
):
    """
    แสดงเฉพาะประวัติการพิมพ์จริง (upload / storage)
    ไม่รวม octoprint ที่เป็น internal job
    """
    emp = _emp(current.employee_id)
    qry = (
        db.query(PrintJob)
        .filter(
            PrintJob.employee_id == emp,
            PrintJob.source.in_(["upload", "storage"]),
            PrintJob.status.in_(["completed", "failed", "canceled"]),
        )
    )

    if q and q.strip():
        text = f"%{q.strip()}%"
        try:
            qry = qry.filter(PrintJob.name.ilike(text))
        except Exception:
            qry = qry.filter(func.lower(PrintJob.name).like(text.lower()))

    rows = (
        qry.order_by(PrintJob.uploaded_at.desc(), PrintJob.id.desc())
        .limit(limit)
        .all()
    )

    return [_job_to_out(db, emp, j) for j in rows]


# -------------------------------------------------------------------
# DELETE /history/{job_id}
# -------------------------------------------------------------------

@router.delete("/{job_id}")
def delete_my_job(
    job_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    job = db.query(PrintJob).filter(PrintJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Not found")

    if (job.employee_id != current.employee_id) and (not current.can_manage_queue):
        raise HTTPException(status_code=403, detail="Forbidden")

    if job.status in ("queued", "processing", "paused"):
        raise HTTPException(status_code=409, detail="Cannot delete an active job")

    db.delete(job)
    db.commit()
    return {"ok": True, "deleted_id": job_id}


# -------------------------------------------------------------------
# DELETE /history (bulk)
# -------------------------------------------------------------------

@router.delete("")
def delete_my_history_bulk(
    older_than_days: Optional[int] = Query(
        None,
        ge=1,
        description="ลบเฉพาะที่เก่ากว่า N วัน (นับจาก finished/uploaded_at). ไม่ใส่ = ลบทั้งหมดที่ไม่ active",
    ),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    emp = _emp(current.employee_id)
    qry = db.query(PrintJob).filter(
        PrintJob.employee_id == emp,
        PrintJob.status.in_(("completed", "failed", "canceled")),
    )

    if older_than_days is not None:
        cutoff = datetime.utcnow() - timedelta(days=older_than_days)
        qry = qry.filter((PrintJob.finished_at == None) | (PrintJob.finished_at < cutoff))  # noqa: E711

    to_delete = qry.all()
    for row in to_delete:
        db.delete(row)
    db.commit()

    return {"ok": True, "deleted": len(to_delete)}


# -------------------------------------------------------------------
# POST /history/merge — ensure storage_files idempotent
# -------------------------------------------------------------------

class HistoryItemIn(BaseModel):
    gcode_key: Optional[str] = None
    gcode_path: Optional[str] = None
    original_key: Optional[str] = None
    name: Optional[str] = None
    uploadedAt: Optional[datetime] = None
    template: Optional[Dict[str, Any]] = None
    stats: Optional[Dict[str, Any]] = None
    file: Optional[Dict[str, Any]] = None
    model_config = ConfigDict(extra="allow")


class HistoryMergeIn(BaseModel):
    items: List[HistoryItemIn] = Field(default_factory=list)


class HistoryMergeOut(BaseModel):
    ok: bool = True
    imported: int
    stored_records: int
    considered: int


@router.post("/merge", response_model=HistoryMergeOut)
def merge_from_client(
    payload: HistoryMergeIn = Body(..., description="items ที่ฝั่ง FE export/migrate ขึ้นมา"),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    items = payload.items or []
    if not items:
        return HistoryMergeOut(ok=True, imported=0, stored_records=0, considered=0)

    emp = _emp(current.employee_id)
    stored = 0
    considered = 0

    for it in items:
        try:
            gk = (it.gcode_key or "").strip()
            ok = (it.original_key or "").strip()

            for candidate in (gk, ok):
                if candidate and candidate.startswith("storage/"):
                    considered += 1
                    filename_hint = (it.name or (it.file or {}).get("name") or candidate).strip() or candidate
                    ensure_storage_record(db, emp, candidate, filename_hint=filename_hint)
                    stored += 1
                    break
        except Exception as e:
            log.warning("merge item failed: %s", e, exc_info=False)
            continue

    db.commit()
    return HistoryMergeOut(ok=True, imported=len(items), stored_records=stored, considered=considered)
