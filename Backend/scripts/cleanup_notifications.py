# scripts/cleanup_notifications.py
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ชี้ไปที่ Backend/users.db (โฟลเดอร์แม่ของ scripts)
DB_PATH = Path(__file__).resolve().parents[1] / "users.db"

# อยากเก็บ noti กี่วัน (เช่น 30 = เก็บ 30 วันล่าสุด)
KEEP_DAYS = 30


def main():
    print("=== cleanup_notifications.py ===")
    print(f"Using DB: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=ON;")
    cur = conn.cursor()

    # คำนวณ cutoff แบบ timezone-aware
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    print(f"Deleting notifications older than: {cutoff_str} (UTC)")

    # ดูก่อนว่ามีกี่แถวที่จะโดนลบ
    cur.execute("SELECT COUNT(*) FROM notifications WHERE created_at < ?", (cutoff_str,))
    to_delete = cur.fetchone()[0]
    print("Notifications to delete:", to_delete)

    if to_delete > 0:
        cur.execute("DELETE FROM notifications WHERE created_at < ?", (cutoff_str,))
        print("Deleted notifications rows:", cur.rowcount)
        conn.commit()

        print("Running VACUUM ...")
        conn.execute("VACUUM;")
    else:
        print("No notifications to delete. Skipping VACUUM.")

    conn.close()
    print("Done.")
    print("===============================")


if __name__ == "__main__":
    main()
