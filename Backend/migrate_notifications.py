import sqlite3, os
db = "users.db"
print("DB:", os.path.abspath(db))
con = sqlite3.connect(db); cur = con.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY,
  ntype TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'info',
  title TEXT NOT NULL,
  message TEXT NULL,
  data_json TEXT NULL,
  created_at TEXT NOT NULL
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS notification_targets (
  id INTEGER PRIMARY KEY,
  notification_id INTEGER NOT NULL,
  employee_id TEXT NOT NULL,
  read_at TEXT NULL,
  FOREIGN KEY(notification_id) REFERENCES notifications(id) ON DELETE CASCADE
);
""")
cur.execute("CREATE INDEX IF NOT EXISTS idx_ntf_target_emp ON notification_targets(employee_id);")
cur.execute("CREATE INDEX IF NOT EXISTS idx_ntf_created ON notifications(created_at);")

con.commit(); con.close()
print("✅ created/verified notifications tables")
