# backend/print_history.py
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from models import User, PrintJob
from print_queue import _ensure_storage_record as ensure_storage_record  # idempotent helper

router = APIRouter(prefix="/history", tags=["history"])
log = logging.getLogger("history")


# -------------------------------------------------------------------
# helpers / json utils
# -------------------------------------------------------------------
def _emp(x: object) -> str:
    return (str(x or "")).strip()


def _to_dict(v: Any) -> Optional[dict]:
    """Accepts dict / JSON string / SQLAlchemy-JSON-like -> dict | None"""
    if v is None:
        return None
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return None
    try:
        return dict(v)  # Mapping-like
    except Exception:
        return None


def _pick(*vals: Optional[str]) -> Optional[str]:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _derive_name_from_key(key: str) -> str:
    base = os.path.basename(key)
    root, _ = os.path.splitext(base)
    return root or base


def _get_from_maps(tj: dict, sj: dict, keys: list[str]) -> Optional[str]:
    """
    ดึงค่าจาก template > stats ตามลำดับ โดยรองรับหลายชื่อคีย์ (synonyms)
    """
    for src in (tj or {}), (sj or {}):
        for k in keys:
            v = src.get(k) if isinstance(src, dict) else None
            if isinstance(v, (str, int, float)) and str(v).strip():
                return str(v).strip()
    return None


# -------------------------------------------------------------------
# preview helpers (BACKEND เป็นคนตัดสิน)
# -------------------------------------------------------------------
def _preview_candidates_from_gcode_key(gk: Optional[str]) -> list[str]:
    """
    สร้างผู้สมัคร key สำหรับไฟล์พรีวิวจาก gcode/object key:
      - ตัด '_oriented' ออก (เคสที่เจอบ่อย)
      - ลองเติม '_preview' และ '_thumb'
      - ลองสลับนามสกุล .png/.jpg/.jpeg
    * ไม่ทำ URL-encode ที่นี่
    """
    if not gk:
        return []
    base, _ext = os.path.splitext(gk)
    dir_ = os.path.dirname(gk)
    base_name = os.path.basename(base)
    base_no_oriented = base_name.replace("_oriented", "")

    exts = [".png", ".jpg", ".jpeg"]
    names: list[str] = []

    # 1) ..._preview.{ext}
    for e in exts:
        names.append(f"{base_name}_preview{e}")
        names.append(f"{base_no_oriented}_preview{e}")

    # 2) ..._thumb.{ext}
    for e in exts:
        names.append(f"{base_name}_thumb{e}")
        names.append(f"{base_no_oriented}_thumb{e}")

    # 3) ...{ext} (ไม่เติม preview/thumb)
    for e in exts:
        names.append(f"{base_name}{e}")
        names.append(f"{base_no_oriented}{e}")

    return [f"{dir_}/{n}" if dir_ else n for n in names]


def _ensure_file_block(job: PrintJob) -> Dict[str, Optional[str]]:
    """Compose 'file' block (เหมาะกับ CustomStore) และคัด preview ฝั่ง backend"""
    fj = _to_dict(getattr(job, "file_json", None)) or _to_dict(getattr(job, "file", None)) or {}
    sj = _to_dict(getattr(job, "stats_json", None)) or _to_dict(getattr(job, "stats", None)) or {}

    gk = _pick(
        fj.get("gcode_key") if isinstance(fj, dict) else None,
        fj.get("object_key") if isinstance(fj, dict) else None,
        fj.get("key") if isinstance(fj, dict) else None,
        sj.get("gcode_key") if isinstance(sj, dict) else None,
        getattr(job, "gcode_key", None),
        getattr(job, "original_key", None),
        getattr(job, "gcode_path", None),
    )

    # ชื่อไฟล์ที่แสดง
    name = None
    if isinstance(fj, dict):
        name = _pick(fj.get("display_name"), fj.get("name"))
    if not name and gk:
        name = _derive_name_from_key(gk)

    # preview ที่ backend จัดให้
    preview_url = None   # URL ตรง (ถ้ามี)
    preview_key = None   # object key (ให้ FE ไป presign)
    if isinstance(fj, dict):
        preview_url = _pick(fj.get("preview_url"), fj.get("preview"))
        preview_key = _pick(fj.get("preview_key"), fj.get("preview_png"), fj.get("thumb"))

    # ถ้าไม่มีทั้งสองอย่าง แต่มี gcode_key → สร้างผู้สมัครชื่อไฟล์ที่เป็นไปได้บ่อย
    if not preview_url and not preview_key and gk:
        cands = _preview_candidates_from_gcode_key(gk)
        if cands:
            preview_key = cands[0]  # เลือกตัวแรก (ไม่เรียก S3 head ที่นี่)

    return {
        "gcode_key": gk,
        "name": name,
        "preview_key": preview_key,
        "preview_url": preview_url,
    }


def _client_job_dict(job: PrintJob, employee_name: str) -> dict:
    """
    Flatten ORM row -> dict for FE.
    Always returns: id, status, time_min, employee_name, uploaded_at/finished_at,
    template{name, layer, walls, infill}, stats{time_min},
    file{gcode_key,name,preview_key,preview_url}
    และเพิ่มฟิลด์ระดับบนสุด: display_name, preview_key, preview_url
    """
    # --- normalize JSON ---
    tj = _to_dict(getattr(job, "template_json", None)) or _to_dict(getattr(job, "template", None)) or {}
    sj = _to_dict(getattr(job, "stats_json", None))    or _to_dict(getattr(job, "stats", None))    or {}

    # เวลา (นาที) : job.time_min > stats.time_min > stats.timeMin > stats.duration_min
    time_min = getattr(job, "time_min", None) or 0
    if not time_min:
        tm = _get_from_maps(tj, sj, ["time_min", "timeMin", "duration_min", "durationMin", "print_time_min"])
        try:
            time_min = int(float(tm)) if tm is not None else 0
        except Exception:
            time_min = 0

    file_block = _ensure_file_block(job)

    # ชื่อแสดงผล: template.name > file.name > จาก gcode_key > id
    display_name = _get_from_maps(tj, sj, ["name", "part_name", "title"])
    if not display_name:
        display_name = _pick(file_block.get("name"))
    if not display_name and file_block.get("gcode_key"):
        display_name = _derive_name_from_key(file_block["gcode_key"])
    if not display_name:
        display_name = str(getattr(job, "id", ""))

    # layer / walls / infill (รองรับหลายชื่อ)
    layer = _get_from_maps(tj, sj, ["layer", "layer_height", "layerHeight", "layer_mm"])
    walls = _get_from_maps(tj, sj, ["walls", "wall_count", "perimeters", "shells"])
    infill = _get_from_maps(tj, sj, ["infill", "infill_percent", "infillPercent", "infill_density", "infillDensity"])

    template_out = {
        "name": display_name,
        "layer": layer,
        "walls": walls,
        "infill": infill,
    }
    stats_out = {"time_min": int(time_min or 0)}

    # ✅ ส่ง field ที่ FE ใช้ที่ระดับบนสุดด้วย
    return {
        "id": getattr(job, "id", None),
        "status": getattr(job, "status", None) or "completed",
        "time_min": stats_out["time_min"],
        "employee_name": employee_name,
        "uploaded_at": getattr(job, "uploaded_at", None).isoformat() if getattr(job, "uploaded_at", None) else None,
        "finished_at": getattr(job, "finished_at", None).isoformat() if getattr(job, "finished_at", None) else None,

        "display_name": display_name,
        "preview_key": file_block.get("preview_key"),
        "preview_url": file_block.get("preview_url"),

        "template": template_out,
        "stats": stats_out,
        "file": file_block,
    }


# -------------------------------------------------------------------
# GET /history/my (+ /my/count)
# -------------------------------------------------------------------
@router.get("/my")
def list_my_history(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    limit: int = Query(200, ge=1, le=1000),
    include_processing: bool = Query(
        False,
        description="true เพื่อรวม queued/paused/processing",
    ),
    since: Optional[datetime] = Query(None, description="กรองตั้งแต่เวลานี้ (uploaded_at/finished_at >= since)"),
):
    emp = _emp(current.employee_id)
    q = db.query(PrintJob).filter(PrintJob.employee_id == emp)

    statuses = ("completed", "failed", "canceled")
    if include_processing:
        statuses = ("completed", "failed", "canceled", "queued", "paused", "processing")
    q = q.filter(PrintJob.status.in_(statuses))

    if since:
        q = q.filter((PrintJob.uploaded_at >= since) | (PrintJob.finished_at >= since))  # type: ignore[operator]

    rows = q.order_by(PrintJob.uploaded_at.desc(), PrintJob.id.desc()).limit(limit).all()

    items: List[dict] = []
    emp_name = current.name or emp
    for j in rows:
        try:
            items.append(_client_job_dict(j, emp_name))
        except Exception as e:
            log.exception("history/_client_job_dict failed id=%s: %s", getattr(j, "id", "?"), e)

    return jsonable_encoder(items, exclude_none=True)


@router.get("/my/count")
def count_my_history(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    include_processing: bool = Query(False),
    since: Optional[datetime] = Query(None),
):
    emp = _emp(current.employee_id)
    q = db.query(PrintJob).filter(PrintJob.employee_id == emp)

    statuses = ("completed", "failed", "canceled")
    if include_processing:
        statuses = ("completed", "failed", "canceled", "queued", "paused", "processing")
    q = q.filter(PrintJob.status.in_(statuses))

    if since:
        q = q.filter((PrintJob.uploaded_at >= since) | (PrintJob.finished_at >= since))  # type: ignore[operator]

    return {"count": q.count()}


# -------------------------------------------------------------------
# DELETE endpoints
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
    q = db.query(PrintJob).filter(
        PrintJob.employee_id == emp,
        PrintJob.status.in_(("completed", "failed", "canceled")),
    )

    if older_than_days is not None:
        cutoff = datetime.utcnow() - timedelta(days=older_than_days)
        q = q.filter((PrintJob.finished_at == None) | (PrintJob.finished_at < cutoff))  # noqa: E711

    to_delete = q.all()
    for row in to_delete:
        db.delete(row)
    db.commit()

    return {"ok": True, "deleted": len(to_delete)}


# -------------------------------------------------------------------
# POST /history/merge (optional: import client-side records for storage files)
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
    payload: HistoryMergeIn = Body(..., description="items จาก FE เพื่อ ensure storage_files"),
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
            # priority: gcode_key > original_key
            for candidate in ((it.gcode_key or "").strip(), (it.original_key or "").strip()):
                if candidate and candidate.startswith("storage/"):
                    considered += 1
                    filename_hint = (it.name or (it.file or {}).get("name") or candidate).strip() or candidate
                    ensure_storage_record(db, emp, candidate, filename_hint=filename_hint)
                    stored += 1
                    break
        except Exception as e:
            log.warning("merge item failed: %s", e, exc_info=False)

    db.commit()
    return HistoryMergeOut(ok=True, imported=len(items), stored_records=stored, considered=considered)
