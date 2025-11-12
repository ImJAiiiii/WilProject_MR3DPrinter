# scripts/cleanup_storage_ghosts.py
from __future__ import annotations

import os
import sys
import argparse
import logging
from pathlib import Path
from typing import Iterable

# ----- Path bootstrap: add Backend/ to sys.path -----
BASE_DIR = Path(__file__).resolve().parents[1]  # -> .../Backend
sys.path.insert(0, str(BASE_DIR))

# ----- Optional: load .env if present -----
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass

# ----- App imports -----
from db import SessionLocal  # type: ignore
from models import StorageFile, PrintJob  # type: ignore
from s3util import head_object  # type: ignore

log = logging.getLogger("cleanup_storage_ghosts")
if not log.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    log.addHandler(h)
log.setLevel(logging.INFO)


def s3_exists(key: str) -> bool:
    """Return True if object exists in S3/MinIO, False otherwise."""
    if not key or key.startswith(("http://", "https://")):
        return False
    try:
        _ = head_object(key)
        return True
    except Exception:
        return False


def find_ghost_storage_files(db) -> list[StorageFile]:
    ghosts: list[StorageFile] = []
    rows: Iterable[StorageFile] = db.query(StorageFile).all()
    for r in rows:
        if not r.object_key or not r.object_key.startswith(("storage/", "catalog/")):
            # แถวหลุดหรือ key แปลก ๆ ก็นับเป็น ghost
            ghosts.append(r)
            continue
        if not s3_exists(r.object_key):
            ghosts.append(r)
    return ghosts


def find_garbage_jobs(db) -> list[PrintJob]:
    """
    กวาด PrintJob แปลก ๆ ที่ทำให้เห็นรายการ 'storage' ใน History:
      - ชื่อ 'storage' และไม่มี gcode_path
      - หรือ gcode_path ชี้ไปที่ object ที่ไม่มีอยู่จริง
    """
    out: list[PrintJob] = []
    jobs: Iterable[PrintJob] = db.query(PrintJob).all()
    for j in jobs:
        name = (j.name or "").strip().lower()
        gk = (j.gcode_path or "").strip()
        if name == "storage" and not gk:
            out.append(j)
            continue
        if gk and gk.startswith(("storage/", "catalog/")) and not s3_exists(gk):
            out.append(j)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Remove ghost StorageFile records and weird jobs that point to missing objects."
    )
    ap.add_argument("--apply", action="store_true", help="ลงมือแก้จริง (ไม่ใส่ = dry-run)")
    ap.add_argument("--verbose", "-v", action="store_true", help="แสดง log เพิ่ม")
    args = ap.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    db = SessionLocal()
    try:
        ghosts = find_ghost_storage_files(db)
        garb_jobs = find_garbage_jobs(db)

        log.info("พบ Storage ghost %d แถว และ PrintJob ขยะ %d แถว", len(ghosts), len(garb_jobs))

        for r in ghosts:
            log.info("[StorageFile] id=%s emp=%s key=%s filename=%s",
                     getattr(r, "id", "?"), getattr(r, "employee_id", "?"),
                     getattr(r, "object_key", "?"), getattr(r, "filename", "?"))
            if args.apply:
                db.delete(r)

        for j in garb_jobs:
            log.info("[PrintJob] id=%s name=%r gcode=%r status=%s",
                     getattr(j, "id", "?"), getattr(j, "name", None),
                     getattr(j, "gcode_path", None), getattr(j, "status", None))
            if args.apply:
                db.delete(j)

        if args.apply:
            db.commit()
            log.info("ลบเรียบร้อย")
        else:
            log.info("dry-run เสร็จ: ยังไม่ลบอะไร (เพิ่ม --apply เพื่อลบจริง)")
    finally:
        db.close()


if __name__ == "__main__":
    main()