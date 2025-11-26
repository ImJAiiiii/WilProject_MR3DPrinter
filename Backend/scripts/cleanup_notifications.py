# scripts/cleanup_notifications.py
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "users.db"
KEEP_DAYS = 30  # หรือ 60/90 ตามที่อยากเก็บ

def main():
    print(f"Using DB: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=ON;")
    cur = conn.cursor()

    # วันที่ cutoff
    cutoff = datetime.utcnow() - timedelta(days=KEEP_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    print(f"Deleting notifications older than: {cutoff_str} (UTC)")

    # ดูจำนวนที่จะลบก่อน
    cur.execute("SELECT COUNT(*) FROM notifications WHERE created_at < ?", (cutoff_str,))
    to_delete = cur.fetchone()[0]
    print("Notifications to delete:", to_delete)

    if to_delete > 0:
        cur.execute("DELETE FROM notifications WHERE created_at < ?", (cutoff_str,))
        print("Deleted notifications rows:", cur.rowcount)

    conn.commit()

    # จัดระเบียบไฟล์ DB ให้เล็กลง
    print("Running VACUUM ...")
    conn.execute("VACUUM;")
    conn.close()
    print("Done.")

if __name__ == "__main__":
    main()
