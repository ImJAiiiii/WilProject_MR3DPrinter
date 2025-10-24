# backend/migrate_fix_storage_names.py
import sqlite3, os
from pathlib import Path

db = os.getenv("DB_PATH", "./users.db")
p = Path(db).resolve()
print("DB:", p)

con = sqlite3.connect(str(p))
cur = con.cursor()

# 1) เติม name จาก basename(object_key) เมื่อ name ไม่มีนามสกุล
cur.execute("""
UPDATE storage_files
   SET name = substr(object_key, instr(object_key, '/')+1)
 WHERE (name IS NULL OR name NOT LIKE '%.gcode' AND name NOT LIKE '%.gco' AND name NOT LIKE '%.gc')
""")

# 2) sync name_low = lower(name)
cur.execute("""
UPDATE storage_files SET name_low = lower(name)
""")

con.commit()
con.close()
print("✅ fixed names & name_low")
