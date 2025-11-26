# scripts/cleanup_notifications.py
import sqlite3
<<<<<<< HEAD
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "users.db"
KEEP_DAYS = 30  # หรือ 60/90 ตามที่อยากเก็บ

def main():
    print(f"Using DB: {DB_PATH}")
=======
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ชี้ไปที่ Backend/users.db (โฟลเดอร์แม่ของ scripts)
DB_PATH = Path(__file__).resolve().parents[1] / "users.db"

# อยากเก็บ noti กี่วัน (เช่น 30 = เก็บ 30 วันล่าสุด)
KEEP_DAYS = 30


def main():
    print("=== cleanup_notifications.py ===")
    print(f"Using DB: {DB_PATH}")

>>>>>>> 9ecec3e6ea86781b1d3b2ab5a829b9bc50a566c2
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=ON;")
    cur = conn.cursor()

<<<<<<< HEAD
    # วันที่ cutoff
    cutoff = datetime.utcnow() - timedelta(days=KEEP_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    print(f"Deleting notifications older than: {cutoff_str} (UTC)")

    # ดูจำนวนที่จะลบก่อน
=======
    # คำนวณ cutoff แบบ timezone-aware
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    print(f"Deleting notifications older than: {cutoff_str} (UTC)")

    # ดูก่อนว่ามีกี่แถวที่จะโดนลบ
>>>>>>> 9ecec3e6ea86781b1d3b2ab5a829b9bc50a566c2
    cur.execute("SELECT COUNT(*) FROM notifications WHERE created_at < ?", (cutoff_str,))
    to_delete = cur.fetchone()[0]
    print("Notifications to delete:", to_delete)

    if to_delete > 0:
        cur.execute("DELETE FROM notifications WHERE created_at < ?", (cutoff_str,))
        print("Deleted notifications rows:", cur.rowcount)
<<<<<<< HEAD

    conn.commit()

    # จัดระเบียบไฟล์ DB ให้เล็กลง
    print("Running VACUUM ...")
    conn.execute("VACUUM;")
    conn.close()
    print("Done.")
=======
        conn.commit()

        print("Running VACUUM ...")
        conn.execute("VACUUM;")
    else:
        print("No notifications to delete. Skipping VACUUM.")

    conn.close()
    print("Done.")
    print("===============================")

>>>>>>> 9ecec3e6ea86781b1d3b2ab5a829b9bc50a566c2

if __name__ == "__main__":
    main()
