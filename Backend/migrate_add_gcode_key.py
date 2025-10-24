# backend/migrate_add_gcode_key.py
from __future__ import annotations
import argparse
import os
import sqlite3
from pathlib import Path

def pick_db_path(cli_db: str | None) -> Path:
    if cli_db:
        return Path(cli_db).resolve()

    # ลองอ่านจาก DATABASE_URL (เช่น sqlite:///app.db)
    url = os.getenv("DATABASE_URL", "").strip()
    if url.startswith("sqlite:///"):
        return Path(url.replace("sqlite:///", "", 1)).resolve()
    if url.startswith("sqlite://"):
        return Path(url.replace("sqlite://", "", 1)).resolve()

    # ดีฟอลต์เดิมของโปรเจ็กต์นี้มักเป็น app.db ในโฟลเดอร์ backend
    return Path(__file__).with_name("app.db").resolve()

def column_exists(cur, table, col) -> bool:
    cur.execute(f"PRAGMA table_info({table})")
    return any(r[1] == col for r in cur.fetchall())

def index_exists(cur, table, name) -> bool:
    cur.execute(f"PRAGMA index_list({table})")
    return any(r[1] == name for r in cur.fetchall())

def main():
    ap = argparse.ArgumentParser(description="Add print_jobs.gcode_key to SQLite DB")
    ap.add_argument("--db", help="path to sqlite database file (e.g. app.db)")
    args = ap.parse_args()

    db_path = pick_db_path(args.db)
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    con = sqlite3.connect(str(db_path))
    cur = con.cursor()

    # 1) เพิ่มคอลัมน์ gcode_key ถ้ายังไม่มี
    if not column_exists(cur, "print_jobs", "gcode_key"):
        print("Adding column print_jobs.gcode_key ...")
        cur.execute("ALTER TABLE print_jobs ADD COLUMN gcode_key TEXT")
        # migrate ค่าจาก gcode_path → gcode_key สำหรับเรคคอร์ดเก่า
        try:
            cur.execute("""
                UPDATE print_jobs
                   SET gcode_key = gcode_path
                 WHERE (gcode_key IS NULL OR gcode_key = '')
                   AND gcode_path LIKE 'storage/%'
            """)
        except Exception:
            pass
        con.commit()
    else:
        print("Column gcode_key already exists.")

    # 2) สร้าง index (ถ้ายังไม่มี)
    if not index_exists(cur, "print_jobs", "ix_print_jobs_gcode_key"):
        print("Creating index ix_print_jobs_gcode_key ...")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_print_jobs_gcode_key ON print_jobs (gcode_key)")
        con.commit()
    else:
        print("Index ix_print_jobs_gcode_key already exists.")

    con.close()
    print(f"Done. Updated DB: {db_path}")

if __name__ == "__main__":
    main()
