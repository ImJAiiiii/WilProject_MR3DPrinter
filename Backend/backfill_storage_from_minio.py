# /backfill_storage_from_minio.py
from __future__ import annotations
import json
from typing import Optional, Dict

from sqlalchemy.orm import Session

from db import SessionLocal  # <- ใช้ของโปรเจกต์คุณ
from models import StorageFile, User
from s3util import list_objects, head_object, get_object_range

CATALOG_PREFIX = "catalog/"

def _parse_meta(meta_bytes: bytes) -> Optional[Dict[str, str]]:
    """
    รองรับทั้ง:
      {"uploader":{"employee_id":"123456","name":"Alice","email":"a@x"}}
    หรือ  {"employee_id":"123456","name":"Alice","email":"a@x"}
    """
    try:
        data = json.loads(meta_bytes.decode("utf-8", errors="ignore"))
        if not isinstance(data, dict):
            return None
        up = data.get("uploader") if isinstance(data.get("uploader"), dict) else data
        emp = (up.get("employee_id") or up.get("emp") or "").strip()
        name = (up.get("name") or "").strip()
        email = (up.get("email") or "").strip()
        if not (emp or name):
            return None
        return {"employee_id": emp or None, "name": name or None, "email": email or None}
    except Exception:
        return None

def _first_json_key(keys):
    for k in keys:
        if k.lower().endswith(".json"):
            return k
    return None

def _first_gcode_key(keys):
    for k in keys:
        kl = k.lower()
        if kl.endswith(".gcode") or kl.endswith(".gco") or kl.endswith(".gc"):
            return k
    return None

def run_backfill(dry_run: bool = True):
    """
    สแกน CATALOG ทั้งหมด กลุ่มตามโฟลเดอร์ชั้น 2,
    หา gcode หลัก และ meta.json ข้างๆ แล้วอัปเดต/แทรก StorageFile
    """
    objs = list_objects(Prefix=CATALOG_PREFIX) or []
    # group by `catalog/<Model>/<Piece>/`
    groups = {}
    for o in objs:
        key = o.get("Key") or ""
        parts = key.split("/")
        if len(parts) < 3:  # catalog/<Model>/...
            continue
        model = parts[1]
        piece = parts[2] if len(parts) >= 4 else ""  # ว่างกรณีไฟล์อยู่ราก model
        group_id = f"catalog/{model}/{piece}/" if piece else f"catalog/{model}/"
        groups.setdefault(group_id, []).append(key)

    print(f"Found {len(groups)} groups under {CATALOG_PREFIX}")

    with SessionLocal() as db:  # type: Session
        inserted = 0
        updated = 0
        skipped = 0
        missing_owner = 0

        for gid, keys in groups.items():
            gcode_key = _first_gcode_key(sorted(keys))
            if not gcode_key:
                skipped += 1
                continue

            # meta
            meta_key = _first_json_key(sorted(keys))
            meta = None
            if meta_key:
                try:
                    meta_bytes = get_object_range(meta_key, start=0, length=200_000)
                    meta = _parse_meta(meta_bytes)
                except Exception:
                    meta = None

            # ลอง head เพื่อได้ size/content_type
            size = None
            ctype = None
            try:
                h = head_object(gcode_key)
                size = int(h.get("ContentLength") or 0)
                ctype = h.get("ContentType") or None
            except Exception:
                pass

            # ถอดชื่อไฟล์โชว์
            filename = gcode_key.split("/")[-1]
            display_name = filename.rsplit(".", 1)[0] or filename

            # หา row เดิมตาม object_key
            row: Optional[StorageFile] = db.query(StorageFile).filter(
                StorageFile.object_key == gcode_key
            ).first()

            emp = (meta or {}).get("employee_id")
            name = (meta or {}).get("name")
            email = (meta or {}).get("email")

            if not row:
                if not emp:
                    # ไม่มี emp → สร้างไม่ได้เพราะ employee_id เป็น NOT NULL
                    missing_owner += 1
                    print(f"[MISS-OWNER] {gcode_key} (no employee_id in meta.json) — skipping")
                    continue

                if dry_run:
                    print(f"[INSERT] {gcode_key} emp={emp} name={name!r}")
                    inserted += 1
                    continue

                row = StorageFile(
                    employee_id=emp,
                    filename=filename,
                    name=display_name,
                    object_key=gcode_key,
                    content_type=ctype,
                    size=size,
                )
                db.add(row)
                inserted += 1
            else:
                # มี row แล้ว → อัปเดต name/ctype/size ให้สมบูรณ์ (กรณีว่าง)
                changed = False
                if not row.name:
                    row.name = display_name
                    changed = True
                if size and not row.size:
                    row.size = size
                    changed = True
                if ctype and not row.content_type:
                    row.content_type = ctype
                    changed = True
                if changed:
                    if dry_run:
                        print(f"[UPDATE] {gcode_key} (fill missing fields)")
                        updated += 1
                    else:
                        updated += 1

            # อัปเกรดชื่อจริงผู้ใช้ใน users ถ้ามี emp + meta.name/email
            if emp and (name or email):
                u = db.query(User).filter(User.employee_id == emp).first()
                if u:
                    changed = False
                    if name and (not u.name or u.name.strip() == emp):
                        u.name = name
                        changed = True
                    if email and not u.email:
                        u.email = email
                        changed = True
                    if changed and dry_run:
                        print(f"[USER-UPDATE] {emp} -> name={u.name!r}, email={u.email!r}")

        if not dry_run:
            db.commit()

    print(f"Done. inserted={inserted}, updated={updated}, skipped={skipped}, missing_owner={missing_owner}")
    if dry_run:
        print("DRY-RUN only. Re-run with dry_run=False to commit.")

if __name__ == "__main__":
    # ขั้นแรกลอง dry-run เพื่อตรวจสอบก่อน
    run_backfill(dry_run=True)
