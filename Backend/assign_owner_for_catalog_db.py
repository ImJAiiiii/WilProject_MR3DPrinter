# /assign_owner_for_catalog_db.py
from __future__ import annotations

import argparse
import re
from typing import Optional, Dict, List, Tuple, Set
from sqlalchemy.orm import Session

from db import SessionLocal
from models import StorageFile, User
from s3util import list_objects, head_object

CATALOG_PREFIX = "catalog/"

# =============================== Name helpers ===============================

def _name_low(s: str) -> str:
    return (s or "").strip().lower()

def _v_match(name: str) -> Tuple[str, Optional[int]]:
    """
    split base + version:
      "flexicat_V12" -> ("flexicat", 12)
      "CameraMounting_V1" -> ("CameraMounting", 1)
      "foo" -> ("foo", None)
    """
    m = re.match(r"^(?P<stem>.+?)_v(?P<n>\d+)$", name.strip(), re.IGNORECASE)
    if not m:
        return name.strip(), None
    return m.group("stem"), int(m.group("n"))

def _load_used_names(db: Session) -> Set[str]:
    """
    โหลด name_low ทั้งระบบ (global) สำหรับกันชื่อซ้ำ
    """
    rows = db.query(StorageFile.name_low).all()
    return {r[0] for r in rows if r and r[0]}

def _load_used_names_by_emp(db: Session, emp: str) -> Set[str]:
    """
    โหลด name_low เฉพาะของพนักงาน emp (กันซ้ำ per-emp ตามคอนสเตรนใน DB)
    """
    rows = db.query(StorageFile.name_low).filter(StorageFile.employee_id == emp).all()
    return {r[0] for r in rows if r and r[0]}

def _next_free_version_name(
    base_name: str,
    used_global: Set[str],
    used_emp: Set[str],
    reservations: Set[str],
) -> str:
    """
    สร้างชื่อที่ไม่ชน 'ทั้งระบบ' และ 'ของพนักงาน' และยังไม่ชนกับชื่อที่เรากำลังจะ insert
    ในรันเดียวกัน (reservations)
    - ถ้า base_name ว่างอยู่ทุกเงื่อนไข -> ใช้เลย
    - ถ้าชน -> ถ้า base_name มี _Vn อยู่แล้วจะเริ่มจาก n+1, ถ้าไม่มีจะเริ่มที่ V2
    """
    base_name = (base_name or "").strip()
    if not base_name:
        base_name = "model_V1"

    low = _name_low(base_name)
    if low not in used_global and low not in reservations and low not in used_emp:
        reservations.add(low)
        return base_name

    stem, n = _v_match(base_name)
    start = (n + 1) if n is not None else 2

    for i in range(start, start + 1000):
        cand = f"{stem}_V{i}"
        low_cand = _name_low(cand)
        if (low_cand not in used_global) and (low_cand not in reservations) and (low_cand not in used_emp):
            reservations.add(low_cand)
            return cand

    # ถ้าหาที่ว่างไม่ได้จริง ๆ (ผิดปกติมาก) ก็คืนชื่อเดิมให้ล้มที่ DB เพื่อเห็นปัญหา
    return base_name

# =============================== S3 grouping ===============================

def _group_by_piece(keys: List[str]) -> Dict[str, Dict]:
    """
    group_id: catalog/<Model>/<Piece>/  (หรือ catalog/<Model>/ ถ้าไฟล์อยู่ราก)
    เก็บ gcode_key เป็นตัวแทน (ถ้าชิ้นหนึ่งมีหลายไฟล์หยิบ gcode ตัวแรกพอ)
    """
    groups: Dict[str, Dict] = {}
    for key in keys:
        if not key.startswith(CATALOG_PREFIX):
            continue
        parts = key.split("/")
        if len(parts) < 3:
            continue
        model = parts[1]
        piece = parts[2] if len(parts) >= 4 else ""
        group_id = f"catalog/{model}/{piece}/" if piece else f"catalog/{model}/"

        g = groups.setdefault(group_id, {
            "model": model,
            "piece": piece or None,
            "gcode_key": None,
        })
        kl = key.lower()
        if kl.endswith(".gcode") or kl.endswith(".gco") or kl.endswith(".gc"):
            if g["gcode_key"] is None:
                g["gcode_key"] = key
    return groups

def _head_safe(key: str):
    try:
        return head_object(key)
    except Exception:
        return {}

def list_missing(db: Session) -> List[Dict]:
    """
    คืนรายการชิ้นงานใน catalog ที่ยังไม่มีแถวใน storage_files (เทียบด้วย object_key)
    """
    keys = [o.get("Key") for o in (list_objects(Prefix=CATALOG_PREFIX) or []) if o.get("Key")]
    groups = _group_by_piece(keys)
    missing = []
    for gid, g in groups.items():
        gk = g["gcode_key"]
        if not gk:
            continue
        row = db.query(StorageFile).filter(StorageFile.object_key == gk).first()
        if not row:
            # ยังไม่มีใน DB = ยังไม่มีเจ้าของ
            h = _head_safe(gk)
            size = h.get("ContentLength")
            ctype = h.get("ContentType")
            filename = gk.split("/")[-1]
            display = filename.rsplit(".", 1)[0] or filename
            missing.append({
                "group_id": gid,
                "object_key": gk,
                "filename": filename,          # มีนามสกุล
                "display_name": display,       # ไม่มีนามสกุล
                "size": int(size or 0),
                "content_type": ctype or None,
            })
    return missing

# =============================== user helper ===============================

def ensure_user(db: Session, emp: str, name_hint: Optional[str] = None):
    emp = (emp or "").strip()
    if not emp:
        return
    u = db.query(User).filter(User.employee_id == emp).first()
    if not u:
        u = User(employee_id=emp, name=name_hint or emp, confirmed=False)
        db.add(u)
        db.flush()  # ยังไม่ commit

# =============================== assign functions ===============================

def assign_owner_for_all(emp: str, dry_run: bool = True):
    """
    กำหนดเจ้าของให้ทุกชิ้นที่ยังไม่มีแถวใน storage_files
    - กันชื่อซ้ำ 'ทั้งระบบ' และ 'ต่อพนักงาน'
    - ดันเวอร์ชัน _Vn อัตโนมัติ
    """
    with SessionLocal() as db:
        missing = list_missing(db)
        if not missing:
            print("Nothing to assign. All catalog items already in DB.")
            return

        print(f"Missing owner records: {len(missing)}")
        for m in missing:
            print(f"- {m['object_key']}  (name={m['display_name']}, size={m['size']})")

        ensure_user(db, emp)

        # โหลดชื่อที่ถูกใช้ไปแล้ว
        used_global = _load_used_names(db)           # ทั้งระบบ
        used_emp = _load_used_names_by_emp(db, emp)  # ต่อพนักงาน
        reservations: Set[str] = set()               # กันซ้ำในรอบรันนี้

        inserted = 0
        for m in missing:
            base_name = m["display_name"]
            unique_name = _next_free_version_name(base_name, used_global, used_emp, reservations)

            if dry_run:
                print(f"[INSERT] emp={emp} key={m['object_key']} name={unique_name!r}")
                inserted += 1
                continue

            db.add(StorageFile(
                employee_id=emp,
                filename=m["filename"],     # มีนามสกุล
                name=unique_name,           # ไม่มีนามสกุล
                object_key=m["object_key"],
                content_type=m["content_type"],
                size=m["size"],
            ))
            # อัปเดต used set ให้สะท้อนของใหม่ (แม้ยังไม่ commit)
            used_global.add(_name_low(unique_name))
            used_emp.add(_name_low(unique_name))
            inserted += 1

        if dry_run:
            print("DRY-RUN: no DB changes committed.")
        else:
            db.commit()
            print(f"Inserted {inserted} storage_files rows.")

def assign_owner_by_piece_map(pairs: List[tuple[str, str]], dry_run: bool = True):
    """
    pairs: list of (piece_path, emp)
      - piece_path รูปแบบ: catalog/<Model>/<Piece>/ (ต้องมีท้าย /)
    จะกำหนดเจ้าของเฉพาะชิ้นที่อยู่ใต้ group นั้น (ถ้ายังไม่มีใน DB)
    - กันชื่อซ้ำทั้งระบบ + ต่อพนักงาน + ดันเวอร์ชันอัตโนมัติ
    """
    with SessionLocal() as db:
        keys = [o.get("Key") for o in (list_objects(Prefix=CATALOG_PREFIX) or []) if o.get("Key")]
        groups = _group_by_piece(keys)

        # เตรียม used set ต่อ emp ที่จะพบใน pairs (ลด query ซ้ำ)
        used_global = _load_used_names(db)
        used_emp_cache: Dict[str, Set[str]] = {}
        reservations: Set[str] = set()

        for piece_path, emp in pairs:
            if not piece_path.endswith("/"):
                piece_path = piece_path + "/"

            g = groups.get(piece_path)
            if not g or not g.get("gcode_key"):
                print(f"[SKIP] {piece_path} not found / no gcode.")
                continue

            gk = g["gcode_key"]
            row = db.query(StorageFile).filter(StorageFile.object_key == gk).first()
            if row:
                print(f"[EXISTS] {gk} already has DB row, skip.")
                continue

            ensure_user(db, emp)

            # ใช้ cache per-emp
            if emp not in used_emp_cache:
                used_emp_cache[emp] = _load_used_names_by_emp(db, emp)

            h = _head_safe(gk)
            size = int(h.get("ContentLength") or 0)
            ctype = h.get("ContentType") or None
            filename = gk.split("/")[-1]
            display = filename.rsplit(".", 1)[0] or filename

            unique_name = _next_free_version_name(display, used_global, used_emp_cache[emp], reservations)

            if dry_run:
                print(f"[INSERT] {piece_path} -> emp={emp}, key={gk}, name={unique_name!r}")
                # จองชื่อไว้ในรอบรันนี้
                used_global.add(_name_low(unique_name))
                used_emp_cache[emp].add(_name_low(unique_name))
                continue

            db.add(StorageFile(
                employee_id=emp,
                filename=filename,
                name=unique_name,
                object_key=gk,
                content_type=ctype,
                size=size,
            ))
            used_global.add(_name_low(unique_name))
            used_emp_cache[emp].add(_name_low(unique_name))

        if dry_run:
            print("DRY-RUN: no DB changes committed.")
        else:
            db.commit()
            print("Done.")

# ================================== CLI ==================================

def main():
    ap = argparse.ArgumentParser(description="Assign owner for catalog files into DB.")
    ap.add_argument("--emp", help="employee_id to assign to ALL missing items")
    ap.add_argument("--piece", action="append",
                    help="assign specific piece path with format 'catalog/<Model>/<Piece>/' or without trailing slash, use with --emp")
    ap.add_argument("--commit", action="store_true", help="commit changes (default is dry-run)")
    args = ap.parse_args()

    if args.piece and not args.emp:
        ap.error("--piece requires --emp")

    if args.piece:
        pairs = [(p, args.emp) for p in args.piece]
        assign_owner_by_piece_map(pairs, dry_run=not args.commit)
    elif args.emp:
        assign_owner_for_all(args.emp, dry_run=not args.commit)
    else:
        # list ให้ดูว่ามีอะไรยังไม่ถูก assign
        with SessionLocal() as db:
            missing = list_missing(db)
            print(f"Missing owner records: {len(missing)}")
            for m in missing:
                print(f"- {m['object_key']}  (name={m['display_name']}, size={m['size']})")
            print("\nTip:")
            print("  python assign_owner_for_catalog_db.py --emp 123456             # dry-run")
            print("  python assign_owner_for_catalog_db.py --emp 123456 --commit    # commit")
            print("  python assign_owner_for_catalog_db.py --emp 123456 --piece catalog/Delta/flexicat_flat_V1/ --commit")

if __name__ == "__main__":
    main()
