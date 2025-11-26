<<<<<<< HEAD
# scripts/cleanup_jobs.py
=======
>>>>>>> 9ecec3e6ea86781b1d3b2ab5a829b9bc50a566c2
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

<<<<<<< HEAD
DB_PATH = Path(__file__).resolve().parent / "users.db"
=======
DB_PATH = Path(file).resolve().parent / "users.db"
>>>>>>> 9ecec3e6ea86781b1d3b2ab5a829b9bc50a566c2
KEEP_DAYS = 365  # เก็บ 1 ปี

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=ON;")
    cur = conn.cursor()

    cutoff = datetime.utcnow() - timedelta(days=KEEP_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    # ลบเฉพาะงานที่จบไปแล้ว (completed/failed/canceled) และเก่ากว่า cutoff
    cur.execute("""
        DELETE FROM print_jobs
        WHERE finished_at IS NOT NULL
          AND finished_at < ?
          AND status IN ('completed','failed','canceled')
    """, (cutoff_str,))
    print("Deleted old print_jobs:", cur.rowcount)

    conn.commit()
    conn.execute("VACUUM;")
    conn.close()

<<<<<<< HEAD
if __name__ == "__main__":
    main()
=======
if name == "main":
    main()
>>>>>>> 9ecec3e6ea86781b1d3b2ab5a829b9bc50a566c2
