import sqlite3

db = r".\users.db"
con = sqlite3.connect(db)
cur = con.cursor()

cur.execute("SELECT id, object_key FROM storage_files WHERE object_key LIKE 'catalog/Delta/banana_front%';")
rows = cur.fetchall()
print("Found rows:", rows)
cur.execute("DELETE FROM storage_files WHERE object_key LIKE 'catalog/Delta/banana_front%';")
print("Deleted rows:", cur.rowcount)

con.commit()
con.execute("VACUUM;")
con.close()
print("Done.")
