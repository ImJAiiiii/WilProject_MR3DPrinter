# backend/gcode_manifest.py
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from s3util import head_object, put_object, presign_get

MANIFEST_VERSION = 1

def manifest_key_for(gcode_key: str) -> str:
    """
    สร้าง path .json ให้ 'อยู่โฟลเดอร์เดียวกัน ชื่อเดียวกัน' กับ .gcode
    เช่น storage/delta/abc123_model.gcode -> storage/delta/abc123_model.json
    """
    base, _ = os.path.splitext(gcode_key)
    return f"{base}.json"

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def build_manifest(
    *,
    gcode_key: str,
    job_name: Optional[str] = None,
    model: Optional[str] = None,
    info: Optional[Dict[str, Any]] = None,     # จาก parse_info(...)
    applied: Optional[Dict[str, Any]] = None,  # จาก requested/applied
    preview: Optional[Dict[str, Any]] = None,  # {image_key, width, height, content_type}
    extra: Optional[Dict[str, Any]] = None,    # ช่องทางใส่ metadata เพิ่ม
) -> Dict[str, Any]:
    """
    ประกอบ payload manifest ให้พร้อมอัปโหลด
    - จะพยายามดึง size/etag/content_type ของ G-code จาก S3 (head_object)
    """
    info = info or {}
    applied = applied or {}
    extra = extra or {}

    size = None
    etag = None
    content_type = "text/x.gcode"
    try:
        h = head_object(gcode_key)
        size = int(h.get("ContentLength") or 0)
        etag = h.get("ETag")
        content_type = h.get("ContentType") or content_type
    except Exception:
        pass

    manifest: Dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "created_at": _utcnow_iso(),
        "name": job_name,
        "model": model,
        "gcode": {
            "key": gcode_key,
            "size": size,
            "etag": etag,
            "content_type": content_type,
        },
        "estimate": {
            "minutes": info.get("estimate_min"),
            "text": info.get("total_text"),
        },
        "filament": {
            "grams": info.get("filament_g"),
        },
        "first_layer": {
            "time_text": info.get("first_layer_time_text") or info.get("first_layer"),
            "time_min": info.get("first_layer_time_min"),
            "height": info.get("first_layer_height"),
        },
        "applied": {
            "fill_density": applied.get("fill_density"),
            "perimeters": applied.get("perimeters"),
            "support": applied.get("support"),
            "layer_height": applied.get("layer_height"),
            "nozzle": applied.get("nozzle"),
            "presets": applied.get("presets"),
        },
        "preview": preview or None,  # เก็บเฉพาะ key/ขนาด/ชนิด ถ้ามีไฟล์ภาพสำรองไว้ที่อื่น
        "extra": extra or None,
    }
    # ล้างคีย์ที่เป็น None ซ้อนระดับเดียว (สวยงามเวลาอ่าน)
    manifest["applied"] = {k: v for k, v in manifest["applied"].items() if v is not None}
    if not manifest["preview"]:
        manifest.pop("preview")
    if not manifest["extra"]:
        manifest.pop("extra")
    return manifest

def write_manifest_for_gcode(
    *,
    gcode_key: str,
    job_name: Optional[str],
    model: Optional[str],
    info: Optional[Dict[str, Any]],
    applied: Optional[Dict[str, Any]],
    preview: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
    cache_control: Optional[str] = "no-cache",
) -> Dict[str, Any]:
    """
    สร้าง + อัปโหลด manifest .json ไปไว้ 'ข้างๆ' ไฟล์ G-code เดิม
    คืนค่า { "key": manifest_key, "url": presigned_get } สำหรับโหลดกลับ
    """
    manifest = build_manifest(
        gcode_key=gcode_key,
        job_name=job_name,
        model=model,
        info=info,
        applied=applied,
        preview=preview,
        extra=extra,
    )
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    mkey = manifest_key_for(gcode_key)
    put_object(
        object_key=mkey,
        data=manifest_bytes,
        content_type="application/json; charset=utf-8",
        cache_control=cache_control,
    )
    try:
        url = presign_get(mkey)
    except Exception:
        url = None
    return {"key": mkey, "url": url, "bytes": len(manifest_bytes)}

def presign_manifest_for_gcode(gcode_key: str) -> Optional[str]:
    """
    สร้าง presigned GET ให้ .json (ถ้ามี)
    """
    try:
        # ตรวจว่ามีจริงก่อน (เพื่อหลีกเลี่ยงลิงก์เสีย)
        mk = manifest_key_for(gcode_key)
        head_object(mk)
        return presign_get(mk)
    except Exception:
        return None
