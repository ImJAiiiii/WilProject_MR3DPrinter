# backend/catalog.py
from __future__ import annotations
import os
import boto3
from botocore.client import Config as BotoConfig
from fastapi import APIRouter, Depends, HTTPException, Query
from auth import get_current_user
from models import User
from s3util import presign_get  # ใช้ของเดิม
# หมายเหตุ: โครงสร้างข้อมูลคาดหวัง: catalog/<Machine>/<PartId>/{meta.json, preview_*.png, part.gcode}

S3_BUCKET   = os.getenv("S3_BUCKET", "printer-store")
S3_ENDPOINT = os.getenv("S3_ENDPOINT", "http://127.0.0.1:9000")
S3_REGION   = os.getenv("S3_REGION", "us-east-1")
S3_ACCESS   = os.getenv("S3_ACCESS_KEY", "minioadmin")
S3_SECRET   = os.getenv("S3_SECRET_KEY", "minioadmin")
S3_SECURE   = os.getenv("S3_SECURE", "false").lower() == "true"

s3 = boto3.client(
    "s3",
    endpoint_url=S3_ENDPOINT,
    region_name=S3_REGION,
    aws_access_key_id=S3_ACCESS,
    aws_secret_access_key=S3_SECRET,
    config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    use_ssl=S3_SECURE,
)

router = APIRouter(prefix="/catalog", tags=["catalog"])

@router.get("/machines")
def list_machine_types():
    """คืนรายชื่อประเภทเครื่อง = โฟลเดอร์ระดับแรกใต้ catalog/"""
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix="catalog/", Delimiter="/")
    machines = []
    for cp in resp.get("CommonPrefixes", []):
        p = cp["Prefix"].rstrip("/")            # e.g. 'catalog/Hontec'
        if "/" in p:
            machines.append(p.split("/", 1)[1]) # -> 'Hontec'
    return {"machines": machines}

# catalog.py
@router.get("/list")
def list_parts(machine: str = Query(..., description="เช่น Hontec, Delta")):
    prefix = f"catalog/{machine}/"
    parts = []

    # สแกนทุก object ใต้เครื่อง
    token = None
    while True:
        resp = s3.list_objects_v2(
            Bucket=S3_BUCKET, Prefix=prefix,
            ContinuationToken=token) if token else s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith(".json"):          # จับทั้ง meta.json / Camera.meta.json
                url = presign_get(key)
                part_prefix = "/".join(key.split("/")[:-1]) + "/"
                parts.append({"part_prefix": part_prefix, "meta_url": url})
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")

    return {"parts": parts}


@router.get("/presign")
def presign(key: str = Query(..., description="S3 key ใน catalog/...")):
    """ขอ presigned GET สำหรับไฟล์ใน catalog (preview/gcode/stl)"""
    try:
        return {"url": presign_get(key)}
    except Exception as e:
        raise HTTPException(500, f"presign failed: {e}")
