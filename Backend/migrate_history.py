# backend/migrate_history.py
import sqlite3, json

DB = "./users.db"

def has_column(cur, table, col):
    cur.execute(f"PRAGMA table_info({table})")
    return any(r[1] == col for r in cur.fetchall())

with sqlite3.connect(DB) as con:
    cur = con.cursor()

    # เพิ่มคอลัมน์ใหม่ (TEXT เก็บ JSON เป็นสตริง ปลอดภัยกับ SQLite)
    if not has_column(cur, "print_jobs", "template_json"):
        cur.execute("ALTER TABLE print_jobs ADD COLUMN template_json TEXT")
    if not has_column(cur, "print_jobs", "stats_json"):
        cur.execute("ALTER TABLE print_jobs ADD COLUMN stats_json TEXT")
    if not has_column(cur, "print_jobs", "thumb"):
        cur.execute("ALTER TABLE print_jobs ADD COLUMN thumb TEXT")
    if not has_column(cur, "print_jobs", "file_json"):
        cur.execute("ALTER TABLE print_jobs ADD COLUMN file_json TEXT")

    # ดัชนีสำหรับดึงประวัติเร็วขึ้น
    cur.execute("CREATE INDEX IF NOT EXISTS idx_print_jobs_emp_uploaded ON print_jobs(employee_id, uploaded_at)")
    con.commit()

print("OK: migrate_history done")
