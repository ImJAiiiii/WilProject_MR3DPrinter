# backend/migrate_add_name_low.py
from sqlalchemy import text
from db import engine

# กันซ้ำทั้งระบบ (ปรับเป็น employee_id,name_low ถ้าต้องการแบบต่อพนักงาน)
UNIQUE_INDEX_SQL = "CREATE UNIQUE INDEX IF NOT EXISTS uq_storage_files_name_low ON storage_files(name_low)"

def column_exists(conn, table, col) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1].lower() == col.lower() for r in rows)

def run():
    with engine.begin() as conn:
        has_name = column_exists(conn, "storage_files", "name")
        has_name_low = column_exists(conn, "storage_files", "name_low")

        if not has_name:
            conn.execute(text("ALTER TABLE storage_files ADD COLUMN name TEXT"))
            print("[OK] add column name")
            conn.execute(text(
                "UPDATE storage_files SET name = filename "
                "WHERE (name IS NULL OR name='') AND filename IS NOT NULL"
            ))
            print("[OK] backfill name from filename")
        else:
            conn.execute(text(
                "UPDATE storage_files "
                "SET name = COALESCE(NULLIF(name,''), filename) "
                "WHERE name IS NULL OR name=''"
            ))
            print("[OK] ensure name has value")

        if not has_name_low:
            conn.execute(text("ALTER TABLE storage_files ADD COLUMN name_low TEXT"))
            print("[OK] add column name_low")

        conn.execute(text(
            "UPDATE storage_files SET name_low = lower(name) "
            "WHERE name IS NOT NULL AND (name_low IS NULL OR name_low='')"
        ))
        print("[OK] backfill name_low from name")

        conn.execute(text(UNIQUE_INDEX_SQL))
        print("[OK] unique index ready")

if __name__ == "__main__":
    run()
    print("Done.")
