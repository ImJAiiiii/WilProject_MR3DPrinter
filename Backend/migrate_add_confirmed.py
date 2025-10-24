# backend/migrate_add_confirmed.py
import sqlite3, os
db = "users.db"
print("DB:", os.path.abspath(db))
con = sqlite3.connect(db); cur = con.cursor()
cols = {r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()}
if "confirmed" not in cols:
    cur.execute("ALTER TABLE users ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 0")
if "last_login_at" not in cols:
    cur.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT NULL")
con.commit(); con.close()
print("✅ migration ok")
