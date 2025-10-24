# backend/migrate_fix_duplicates_and_index.py
from sqlalchemy import text
from db import engine

# ============ helper ============
def column_exists(conn, table, col) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1].lower() == col.lower() for r in rows)

def fetchall(conn, sql, **kw):
    return conn.execute(text(sql), kw).fetchall()

def execute(conn, sql, **kw):
    conn.execute(text(sql), kw)

def ensure_columns_and_backfill(conn):
    has_name = column_exists(conn, "storage_files", "name")
    has_name_low = column_exists(conn, "storage_files", "name_low")

    if not has_name:
        execute(conn, "ALTER TABLE storage_files ADD COLUMN name TEXT")
        print("[OK] add column name")
        execute(conn, """
            UPDATE storage_files
            SET name = filename
            WHERE (name IS NULL OR name='') AND filename IS NOT NULL
        """)
        print("[OK] backfill name from filename")
    else:
        execute(conn, """
            UPDATE storage_files
            SET name = COALESCE(NULLIF(name,''), filename)
            WHERE name IS NULL OR name=''
        """)
        print("[OK] ensure name has value")

    if not has_name_low:
        execute(conn, "ALTER TABLE storage_files ADD COLUMN name_low TEXT")
        print("[OK] add column name_low")

    execute(conn, """
        UPDATE storage_files
        SET name_low = lower(name)
        WHERE name IS NOT NULL AND (name_low IS NULL OR name_low='')
    """)
    print("[OK] backfill name_low from name")

def find_duplicates(conn):
    rows = fetchall(conn, """
        SELECT name_low, COUNT(*) AS cnt
        FROM storage_files
        GROUP BY name_low
        HAVING cnt > 1
    """)
    return [r[0] for r in rows]

def exists_name_low(conn, name_low: str) -> bool:
    r = fetchall(conn, """
        SELECT 1 FROM storage_files WHERE name_low=:nl LIMIT 1
    """, nl=name_low)
    return len(r) > 0

def bump_version_once(name: str) -> str:
    """
    ถ้าเป็น xxx_V12.ext → xxx_V13.ext
    ถ้าไม่ใช่ pattern → แทรก _V2 ก่อนนามสกุล (หรือท้ายชื่อถ้าไม่มีนามสกุล)
    """
    import re
    m = re.search(r'^(.*?_V)(\d+)(\.[^.]+)?$', name, flags=re.IGNORECASE)
    if m:
        head, num, ext = m.group(1), int(m.group(2)), (m.group(3) or '')
        return f"{head}{num+1}{ext}"
    # ไม่ตรง pattern → ใส่ _V2
    m2 = re.search(r'^(.*?)(\.[^.]+)$', name)  # แยกนามสกุลถ้ามี
    if m2:
        base, ext = m2.group(1), m2.group(2)
        return f"{base}_V2{ext}"
    return f"{name}_V2"

def fix_one_group(conn, name_low: str):
    """
    สำหรับ name_low ที่ซ้ำ: ให้คง “ตัวแรกสุด (uploaded_at เก่าสุด/ id เล็กสุด)” ไว้
    ที่เหลือทำการ bump version ไปเรื่อยๆ จนกว่าจะไม่ซ้ำ
    """
    # ดึงรายการที่ซ้ำ เรียงให้ตัวแรกเป็น “ตัวที่รักษาไว้”
    rows = fetchall(conn, """
        SELECT id, name, name_low, uploaded_at
        FROM storage_files
        WHERE name_low = :nl
        ORDER BY uploaded_at ASC NULLS FIRST, id ASC
    """, nl=name_low)

    if len(rows) <= 1:
        return 0

    keep = rows[0]  # เก็บตัวแรกไว้
    changed = 0
    # จัดการตัวที่ 2..n
    for r in rows[1:]:
        _id, cur_name, _, _ = r
        new_name = cur_name
        # วน bump จนกว่าจะไม่ชน
        tries = 0
        while True:
            tries += 1
            if tries > 100:
                raise RuntimeError(f"Bump loop overflow for id={_id}, start={cur_name}")
            new_name = bump_version_once(new_name)
            nl = new_name.lower()
            if not exists_name_low(conn, nl):
                # อัปเดตฐานข้อมูล
                execute(conn, """
                    UPDATE storage_files
                    SET name = :n, name_low = :nl
                    WHERE id = :id
                """, n=new_name, nl=nl, id=_id)
                changed += 1
                break
    return changed

def dedupe_all(conn):
    total_changed = 0
    while True:
        dups = find_duplicates(conn)
        if not dups:
            break
        print(f"[INFO] duplicate groups: {len(dups)}")
        for nl in dups:
            changed = fix_one_group(conn, nl)
            total_changed += changed
            if changed:
                print(f"  - fixed group '{nl}': +{changed} rename(s)")
        # ทำซ้ำจนไม่มีซ้ำหลุดมาอีก
    print(f"[OK] dedupe done, renamed {total_changed} row(s)")
    return total_changed

def create_unique_index(conn):
    # เผื่อเคยมี unique ต่อพนักงาน ให้ลบทิ้งก่อน (จะไม่ error ถ้าไม่มี)
    execute(conn, "DROP INDEX IF EXISTS uq_storage_files_emp_name_low")
    # แล้วสร้าง unique ทั้งระบบ
    execute(conn, "CREATE UNIQUE INDEX IF NOT EXISTS uq_storage_files_name_low ON storage_files(name_low)")
    print("[OK] unique index on name_low (global) created")

def debug_print(conn):
    # แสดงตัวอย่างข้อมูลตรวจสอบ
    rows = fetchall(conn, """
        SELECT id, name, name_low, filename
        FROM storage_files
        ORDER BY uploaded_at ASC NULLS FIRST, id ASC
        LIMIT 10
    """)
    print("[SAMPLE]")
    for r in rows:
        print("  id=%s | name=%s | name_low=%s | filename=%s" % (r[0], r[1], r[2], r[3]))

def main():
    with engine.begin() as conn:
        ensure_columns_and_backfill(conn)
        dedupe_all(conn)
        create_unique_index(conn)
        debug_print(conn)

if __name__ == "__main__":
    main()
    print("Done.")
