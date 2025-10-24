# /backfill_previews.py
import os
import sys
import time
import json
import re
import requests
import boto3

API_BASE   = os.getenv("API_BASE", "http://localhost:8000")
EMP_ID     = os.getenv("EMPLOYEE_ID", "")   # ใช้เลขพนักงานที่มีสิทธิ์ login
MODEL      = os.getenv("MODEL", "")         # ตัวกรองเช่น "Delta" (ว่าง = ทั้งหมด)
SCAN_STORAGE = os.getenv("SCAN_STORAGE", "0").lower() in ("1","true","yes")  # รวม storage/ ด้วยไหม
IMG_SIZE   = os.getenv("IMG_SIZE", "1600x1100")
SLEEP_SEC  = float(os.getenv("SLEEP_SEC", "0.2"))  # หน่วงระหว่าง job เพื่อเบาเครื่อง

S3_ENDPOINT = os.getenv("S3_ENDPOINT")
S3_REGION   = os.getenv("S3_REGION", "us-east-1")
S3_BUCKET   = os.getenv("S3_BUCKET", "")
S3_SECURE   = os.getenv("S3_SECURE", "false").lower() in ("1","true","yes")

def login(emp_id: str) -> str:
    # เรียก /auth/login (ใช้ employee_id)
    r = requests.post(f"{API_BASE}/auth/login", json={"employee_id": str(emp_id).strip()})
    r.raise_for_status()
    j = r.json()
    # backend ใหม่คืน access_token / refresh_token
    token = j.get("access_token") or j.get("token")
    if not token:
        raise SystemExit("Login failed, no token")
    return token

def has_preview(cli, key: str) -> bool:
    # preview key = ชื่อเดียวกันแต่ลงท้าย .preview.png
    p = key.rsplit("/", 1)[-1]
    stem = p.rsplit(".", 1)[0]
    preview = key[:-len(p)] + f"{stem}.preview.png"
    try:
        cli.head_object(Bucket=S3_BUCKET, Key=preview)
        return True
    except Exception:
        return False

def preview_key_for(key: str) -> str:
    fname = key.rsplit("/", 1)[-1]
    stem  = fname.rsplit(".", 1)[0]
    base  = key[:-len(fname)]
    return base + f"{stem}.preview.png"

def list_gcodes_under(prefix: str):
    sess = boto3.session.Session(
        aws_access_key_id=os.getenv("S3_ACCESS_KEY"),
        aws_secret_access_key=os.getenv("S3_SECRET_KEY"),
        region_name=S3_REGION or None,
    )
    cli = sess.client("s3", endpoint_url=S3_ENDPOINT, verify=S3_SECURE)
    paginator = cli.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            kl = key.lower()
            if kl.endswith(".gcode") or kl.endswith(".gco") or kl.endswith(".gc"):
                yield key

def regenerate_for_list(keys, token):
    headers = {"Authorization": f"Bearer {token}"}
    ok = 0; skip = 0; fail = 0
    for key in keys:
        try:
            res = requests.post(
                f"{API_BASE}/api/storage/preview/regenerate",
                params={"object_key": key, "img_size": IMG_SIZE},
                headers=headers, timeout=120,
            )
            if res.status_code == 200:
                ok += 1
                info = res.json()
                print(f"[OK] {key} -> {info.get('preview_key')}")
            else:
                fail += 1
                print(f"[FAIL] {key} -> {res.status_code} {res.text}")
        except Exception as e:
            fail += 1
            print(f"[FAIL] {key} -> {e}")
        time.sleep(SLEEP_SEC)
    print(f"Done: ok={ok}, fail={fail}, skip={skip}")

def main():
    if not EMP_ID:
        print("Please set EMPLOYEE_ID env.")
        sys.exit(1)

    token = login(EMP_ID)

    # เตรียม S3 client เพื่อตรวจว่า preview มี/ไม่มี
    sess = boto3.session.Session(
        aws_access_key_id=os.getenv("S3_ACCESS_KEY"),
        aws_secret_access_key=os.getenv("S3_SECRET_KEY"),
        region_name=S3_REGION or None,
    )
    cli = sess.client("s3", endpoint_url=S3_ENDPOINT, verify=S3_SECURE)

    targets = []

    # 1) catalog/<Model or All>/
    if MODEL:
        # ให้ backend ใช้ตัวอักษรขึ้นต้นด้วยตัวใหญ่
        prefix = f"catalog/{MODEL.strip().capitalize()}/"
    else:
        prefix = "catalog/"

    print(f"Scanning: s3://{S3_BUCKET}/{prefix}")
    for key in list_gcodes_under(prefix):
        if not has_preview(cli, key):
            targets.append(key)

    # 2) (ออปชัน) storage/<sub>/
    if SCAN_STORAGE:
        # ถ้า MODEL = delta → storage/delta/
        # ถ้า MODEL = hontech → storage/hontech/
        # ถ้า MODEL ว่าง → storage/
        sub = MODEL.strip().lower() if MODEL else ""
        sprefix = f"storage/{sub}/" if sub else "storage/"
        print(f"Scanning: s3://{S3_BUCKET}/{sprefix}")
        for key in list_gcodes_under(sprefix):
            if not has_preview(cli, key):
                targets.append(key)

    print(f"Total missing previews: {len(targets)}")
    if not targets:
        return

    regenerate_for_list(targets, token)

if __name__ == "__main__":
    main()
