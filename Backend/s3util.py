# backend/s3util.py
from __future__ import annotations

import os
import re
import uuid
from typing import Any, Dict, Optional

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

# ==== ENV ====
S3_ENDPOINT   = os.getenv("S3_ENDPOINT", "http://localhost:9000")
S3_REGION     = os.getenv("S3_REGION", "us-east-1")
S3_ACCESS     = os.getenv("S3_ACCESS_KEY", "minioadmin")
S3_SECRET     = os.getenv("S3_SECRET_KEY", "minioadmin")
S3_BUCKET     = os.getenv("S3_BUCKET", "printer-store")
# หมายเหตุ: เราไม่ auto ใส่ S3_PREFIX ให้ทุก object โดยอัตโนมัติ
# เพื่อหลีกเลี่ยง "staging/staging/..." ให้ผู้เรียกกำหนดเองใน key
S3_PREFIX     = os.getenv("S3_PREFIX", "staging/")  # ใช้กับ new_object_key เท่านั้น
S3_SECURE     = os.getenv("S3_SECURE", "false").lower() == "true"
S3_PATHSTYLE  = os.getenv("S3_FORCE_PATH_STYLE", "true").lower() == "true"
PRESIGN_SEC   = int(os.getenv("PRESIGN_EXPIRES", "600"))

# ==== Boto3 client ====
_session = boto3.session.Session(
    aws_access_key_id=S3_ACCESS,
    aws_secret_access_key=S3_SECRET,
    region_name=S3_REGION,
)
_s3 = _session.client(
    "s3",
    endpoint_url=S3_ENDPOINT,
    use_ssl=S3_SECURE,
    config=BotoConfig(
        signature_version="s3v4",
        s3={"addressing_style": "path"} if S3_PATHSTYLE else {"addressing_style": "virtual"},
    ),
)

# ==== Helpers ====
def _ensure_bucket_and_cors() -> None:
    """Ensure bucket exists and apply permissive CORS for local dev."""
    try:
        _s3.head_bucket(Bucket=S3_BUCKET)
    except ClientError as e:
        code = (e.response or {}).get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchBucket", "NotFound"):
            try:
                _s3.create_bucket(
                    Bucket=S3_BUCKET,
                    CreateBucketConfiguration={"LocationConstraint": S3_REGION},
                )
            except ClientError:
                # บาง MinIO ไม่ต้องใส่ LocationConstraint
                _s3.create_bucket(Bucket=S3_BUCKET)
        else:
            raise

    # CORS สำหรับ dev (ล้มได้ไม่เป็นไร)
    try:
        _s3.put_bucket_cors(
            Bucket=S3_BUCKET,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedOrigins": [
                            "http://localhost:3000",
                            "http://127.0.0.1:3000",
                            "http://localhost:5173",
                            "http://127.0.0.1:5173",
                        ],
                        "AllowedMethods": ["GET", "PUT", "POST"],
                        "AllowedHeaders": ["*"],
                        "ExposeHeaders": ["ETag"],
                        "MaxAgeSeconds": 3600,
                    }
                ]
            },
        )
    except Exception:
        pass

_ensure_bucket_and_cors()

_name_clean_re = re.compile(r"[^\w.\-]+")

def _sanitize_filename(name: str) -> str:
    """Keep only [A-Za-z0-9_.-] and clip length."""
    name = _name_clean_re.sub("_", (name or "").strip())
    return name[:150] or "file"

def _validate_key(key: str) -> None:
    """Best-effort object key validation."""
    if not key or key.startswith("/") or ".." in key or "://" in key:
        raise ValueError(f"Invalid S3 object key: {key!r}")

# ==== Key generators ====
def new_object_key(filename: str) -> str:
    """Generic key with S3_PREFIX."""
    base = _sanitize_filename(filename or "file")
    uid  = uuid.uuid4().hex[:12]
    return f"{S3_PREFIX}{uid}_{base}"

def new_staging_key(filename: str) -> str:
    base = _sanitize_filename(filename or "file")
    uid  = uuid.uuid4().hex[:12]
    return f"staging/{uid}_{base}"

def new_storage_key(filename: str) -> str:
    base = _sanitize_filename(filename or "file")
    uid  = uuid.uuid4().hex[:12]
    return f"storage/{uid}_{base}"

# ==== Presign helpers ====
def presign_put(object_key: str, content_type: Optional[str] = None) -> Dict[str, Any]:
    _validate_key(object_key)
    params: Dict[str, Any] = {"Bucket": S3_BUCKET, "Key": object_key}
    headers: Dict[str, str] = {}
    if content_type:
        params["ContentType"] = content_type
        headers["Content-Type"] = content_type
    url = _s3.generate_presigned_url(
        ClientMethod="put_object",
        Params=params,
        ExpiresIn=PRESIGN_SEC,
        HttpMethod="PUT",
    )
    return {
        "object_key": object_key,
        "url": url,
        "method": "PUT",
        "headers": headers,
        "expires_in": PRESIGN_SEC,
    }

def presign_get(object_key: str) -> str:
    _validate_key(object_key)
    return _s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": S3_BUCKET, "Key": object_key},
        ExpiresIn=PRESIGN_SEC,
    )

# ==== Basic ops ====
def head_object(object_key: str) -> Dict[str, Any]:
    _validate_key(object_key)
    return _s3.head_object(Bucket=S3_BUCKET, Key=object_key)

def delete_object(object_key: str) -> None:
    _validate_key(object_key)
    _s3.delete_object(Bucket=S3_BUCKET, Key=object_key)

def copy_object(src_key: str, dst_key: str, content_type: Optional[str] = None) -> None:
    _validate_key(src_key); _validate_key(dst_key)
    kwargs: Dict[str, Any] = {
        "Bucket": S3_BUCKET,
        "Key": dst_key,
        "CopySource": {"Bucket": S3_BUCKET, "Key": src_key},
    }
    # ถ้าต้องการเปลี่ยน content-type ต้อง REPLACE metadata
    if content_type:
        kwargs["ContentType"] = content_type
        kwargs["MetadataDirective"] = "REPLACE"
    _s3.copy_object(**kwargs)

def download_to_file(object_key: str, dest_path: str) -> str:
    _validate_key(object_key)
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    _s3.download_file(S3_BUCKET, object_key, dest_path)
    return dest_path

def upload_bytes(
    data: bytes,
    object_key: str,
    content_type: Optional[str] = None,
    metadata: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    _validate_key(object_key)
    kwargs: Dict[str, Any] = {"Bucket": S3_BUCKET, "Key": object_key, "Body": data}
    if content_type:
        kwargs["ContentType"] = content_type
    if metadata:
        kwargs["Metadata"] = metadata
    resp = _s3.put_object(**kwargs)
    return {"object_key": object_key, "etag": resp.get("ETag")}

# ==== NEW: partial object reader (for parsing G-code header) ====
def get_object_range(object_key: str, start: int = 0, length: int = 128 * 1024) -> bytes:
    """
    อ่านบางส่วนของ object ด้วย Range header.
    ใช้สำหรับอ่านหัวไฟล์ G-code เพื่อดึงเวลา estimate โดยไม่ต้องโหลดทั้งไฟล์
    """
    _validate_key(object_key)
    start = max(0, int(start))
    length = max(1, int(length))
    end = start + length - 1
    resp = _s3.get_object(
        Bucket=S3_BUCKET,
        Key=object_key,
        Range=f"bytes={start}-{end}",
    )
    return resp["Body"].read()
