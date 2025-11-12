# backend/s3util.py
from __future__ import annotations

import os
import re
import uuid
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, List, Iterable, Tuple
from urllib.parse import urlparse, urlunparse

# --- load .env early (safe no-op if not installed) ---
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError, EndpointConnectionError

log = logging.getLogger(__name__)

# ==== ENV & helpers (fix quotes around secrets) ====
def _unquote(v: Optional[str]) -> str:
    if not v:
        return ""
    v = v.strip()
    if (len(v) >= 2) and ((v[0] == v[-1] == "'") or (v[0] == v[-1] == '"')):
        return v[1:-1]
    return v

def _env_bool(name: str, default: bool) -> bool:
    return (os.getenv(name, "true" if default else "false") or "").strip().lower() in ("1", "true", "yes", "y")

S3_ENDPOINT = os.getenv("S3_ENDPOINT", "http://localhost:9000")
S3_REGION = os.getenv("S3_REGION", "us-east-1")
S3_ACCESS = _unquote(os.getenv("S3_ACCESS_KEY", "minioadmin"))
S3_SECRET = _unquote(os.getenv("S3_SECRET_KEY", "minioadmin"))
S3_BUCKET = os.getenv("S3_BUCKET", "printer-store")

# public endpoint (for presigned URL rewrite)
S3_PUBLIC_ENDPOINT = (os.getenv("S3_PUBLIC_ENDPOINT", "") or "").strip()

# NOTE: keep generic prefix for miscellaneous objects only
S3_PREFIX = os.getenv("S3_PREFIX", "staging/")
S3_SECURE = _env_bool("S3_SECURE", False)
S3_PATHSTYLE = _env_bool("S3_FORCE_PATH_STYLE", True)
PRESIGN_SEC = int(os.getenv("PRESIGN_EXPIRES", "600"))

# CORS origins
_CORS_FROM_ENV: List[str] = [
    o.strip() for o in (os.getenv("CORS_ORIGINS", "") or "").split(",") if o.strip()
]
_CORS_DEFAULTS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
_CORS_ORIGINS = _CORS_FROM_ENV or _CORS_DEFAULTS

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

# ==== Helpers (general) ====
# ใช้ ASCII-only เพื่อให้คีย์/โฟลเดอร์ปลอดภัยและสม่ำเสมอบนทุกระบบ
_name_clean_re = re.compile(r"[^A-Za-z0-9_.-]+")
_slug_clean_re = re.compile(r"[^A-Za-z0-9_-]+")
_version_re = re.compile(r"^(.+?)_V(\d+)$", re.I)

def _sanitize_filename(name: str) -> str:
    """Keep only [A-Za-z0-9_.-] and clip length."""
    name = _name_clean_re.sub("_", (name or "").strip())
    return name[:150] or "file"

def _sanitize_slug(s: str) -> str:
    """Slug for folder names (no dots)."""
    s = _slug_clean_re.sub("_", (s or "").strip())
    # บางทีชื่อว่าง ให้ default เป็น 'unknown'
    return s[:100] or "unknown"

def _sanitize_folder(s: str) -> str:
    """Folder-safe slug butอนุญาตจุดในชื่อชิ้นงานได้"""
    s = str(s or "").strip()
    s = re.sub(r"\s+", "_", s)              # space -> _
    s = re.sub(r"[^A-Za-z0-9._-]+", "", s)  # ตัดตัวแปลก
    s = re.sub(r"_+", "_", s)               # ยุบ _ ซ้อน
    return s or "Model"

def _ensure_version(filename: str) -> str:
    """
    ถ้าไฟล์ยังไม่มี suffix แบบ _V# ให้บังคับเติม _V1 ก่อนนามสกุล
    เช่น banana_back.gcode -> banana_back_V1.gcode
    """
    try:
        fn = Path(filename or "file").name
    except Exception:
        fn = filename or "file"
    stem = Path(fn).stem
    ext = Path(fn).suffix
    if not _version_re.match(stem):
        stem = f"{stem}_V1"
    return f"{stem}{ext or ''}"

def _validate_key(key: str) -> None:
    if not key or key.startswith("/") or ".." in key or "://" in key:
        raise ValueError(f"Invalid S3 object key: {key!r}")

def _guess_content_type(name: Optional[str]) -> str:
    n = (name or "").lower()
    if n.endswith((".gcode", ".gco", ".gc")):
        return "text/x.gcode"
    if n.endswith(".stl"):
        return "model/stl"
    if n.endswith(".3mf"):
        return "application/vnd.ms-package.3dmanufacturing-3dmodel"
    if n.endswith(".obj"):
        return "text/plain"
    if n.endswith(".png"):
        return "image/png"
    if n.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if n.endswith(".json"):
        return "application/json; charset=utf-8"
    return "application/octet-stream"

def normalize_s3_prefix(p: str, bucket: Optional[str] = None) -> str:
    if not p:
        return ""
    p = p.replace("\\", "/").lstrip("/")
    if bucket and p.startswith(bucket + "/"):
        p = p[len(bucket) + 1 :]
    if not p.endswith("/"):
        p += "/"
    return p

def build_key(prefix: str, filename: str) -> str:
    return normalize_s3_prefix(prefix, S3_BUCKET) + _sanitize_filename(filename)

def _rewrite_presign_host(url: str) -> str:
    if not url or not S3_PUBLIC_ENDPOINT:
        return url
    try:
        u = urlparse(url)
        p = urlparse(S3_PUBLIC_ENDPOINT)
        scheme = p.scheme or u.scheme or ("https" if S3_SECURE else "http")
        netloc = p.netloc or p.path or u.netloc
        return urlunparse((scheme, netloc, u.path, u.params, u.query, ""))
    except Exception:
        return url

# ==== One-time ensure bucket & CORS ====
def _ensure_bucket_and_cors() -> None:
    try:
        _s3.head_bucket(Bucket=S3_BUCKET)
    except (EndpointConnectionError, ClientError) as e:
        log.warning("S3 head_bucket failed: %s", e)
        return

    try:
        _s3.put_bucket_cors(
            Bucket=S3_BUCKET,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedOrigins": _CORS_ORIGINS,
                        "AllowedMethods": ["GET", "PUT", "POST", "HEAD", "OPTIONS"],
                        "AllowedHeaders": ["*"],
                        "ExposeHeaders": ["ETag", "Content-Length", "Content-Range"],
                        "MaxAgeSeconds": 3600,
                    }
                ]
            },
        )
    except Exception as e:
        log.debug("S3 put_bucket_cors skipped: %s", e)

_ensure_bucket_and_cors()

# ==== Key generators ====
def new_object_key(filename: str) -> str:
    """Generic key with S3_PREFIX (misc objects; keep random to avoid collision)."""
    base = _sanitize_filename(filename or "file")
    uid = uuid.uuid4().hex[:12]
    return f"{S3_PREFIX}{uid}_{base}"

def new_staging_key(filename: str) -> str:
    """Temporary key before finalize (random allowed)."""
    base = _sanitize_filename(filename or "file")
    uid = uuid.uuid4().hex[:12]
    return f"staging/{uid}_{base}"

def new_storage_key(filename: str) -> str:
    """
    Generic permanent object (not model-aware). Keep random to avoid collisions
    when ingesting from /uploads.
    """
    base = _sanitize_filename(filename or "file")
    uid = uuid.uuid4().hex[:12]
    return f"storage/{uid}_{base}"

# === Canonical model name (อิง models.json ถ้ามี) ===
try:
    _MODELS_MAP: Dict[str, str] = {}
    _models_path = os.getenv("MODELS_JSON", "models.json")
    if os.path.exists(_models_path):
        data = json.load(open(_models_path, "r", encoding="utf-8"))
        items = data.get("models", data if isinstance(data, list) else [])
        for m in items:
            title = m.get("title") or m.get("name") or m.get("id")
            if title:
                _MODELS_MAP[title.lower()] = title  # เก็บรูปแบบมาตรฐาน (เช่น "Delta")
except Exception:
    _MODELS_MAP = {}

def _canonical_model(model: str) -> str:
    m = (model or "").strip()
    if not m:
        return "Default"
    return _MODELS_MAP.get(m.lower(), m.title())

# — Model-aware storage/catalog keys WITHOUT hashes —
def _model_sub_storage(model: str) -> str:
    # storage ใช้ lower คงที่
    return _sanitize_slug(_canonical_model(model)).lower()

def _model_sub_catalog(model: str) -> str:
    # catalog ใช้ชื่อแบบ TitleCase เดียวกันเสมอ (รวม Delta/DELTA → Delta)
    return _canonical_model(model)

def new_storage_key_for_model(model: str, filename: str) -> str:
    """
    ✅ No hash: storage/<model-sub>/<filename>
    e.g. storage/delta/CameraMounting_V1.gcode
    """
    sub = _model_sub_storage(model)
    # บังคับมีเวอร์ชัน (_V#) ก่อนเก็บ
    name = _ensure_version(_sanitize_filename(Path(filename or "file").name))
    return f"storage/{sub}/{name}"

def new_catalog_key_for_model(model: str, filename: str) -> str:
    """
    ✅ No hash + per-piece folder (legacy helper — หลีกเลี่ยง ใช้ catalog_paths_for_job แทน):
    catalog/<ModelTitle>/<Stem>/<Filename>
    """
    title = _model_sub_catalog(model)
    # บังคับมีเวอร์ชัน (_V#) ก่อนเก็บ
    name  = _ensure_version(_sanitize_filename(Path(filename or "file").name))
    stem  = _sanitize_slug(Path(name).stem) or "model"
    return f"catalog/{title}/{stem}/{name}"

# ===== Standardized catalog paths for one job (ใช้ jobName เป็น single source) =====
def catalog_paths_for_job(model: str, job_name: str, ext: str = ".gcode") -> Dict[str, str]:
    """
    ผลลัพธ์มาตรฐาน:
      catalog/<ModelTitle>/<JobName>/<JobName>.(gcode|json|preview.png)
    """
    title = _model_sub_catalog(model)
    base  = _sanitize_folder(job_name)
    return {
        "dir":     f"catalog/{title}/{base}/",
        "gcode":   f"catalog/{title}/{base}/{base}{ext}",
        "json":    f"catalog/{title}/{base}/{base}.json",
        "preview": f"catalog/{title}/{base}/{base}.preview.png",
        "base":    base,
        "folder":  base,
    }

# ===== Atomic commit utilities =====
def object_exists(object_key: str) -> bool:
    try:
        head_object(object_key)
        return True
    except Exception:
        return False

def _move_object(src: str, dst: str):
    copy_object(src_key=src, dst_key=dst)
    delete_object(src)

def staging_triple_keys(model: str, job_name: str) -> Dict[str, str]:
    """
    เก็บไฟล์ชั่วคราวที่ staging/catalog/<Model>/<Job>/<uuid>/
    จนครบสามไฟล์ แล้วค่อย commit
    """
    title = _model_sub_catalog(model)
    base  = _sanitize_folder(job_name)
    tmpid = uuid.uuid4().hex[:10]
    prefix = f"staging/catalog/{title}/{base}/{tmpid}/"
    return {
        "prefix":      prefix,
        "gcode_tmp":   f"{prefix}{base}.gcode",
        "json_tmp":    f"{prefix}{base}.json",
        "preview_tmp": f"{prefix}{base}.preview.png",
    }

def is_triple_complete(paths: Dict[str, str]) -> bool:
    return object_exists(paths["gcode"]) and object_exists(paths["json"]) and object_exists(paths["preview"])

def commit_triple_to_catalog(model: str, job_name: str, tmp: Dict[str, str]) -> Dict[str, str]:
    """
    ต้องมี tmp.gcode_tmp/json_tmp/preview_tmp ครบเท่านั้น
    ย้าย (copy+delete) ไปยัง catalog/<Model>/<Job>/<Job>.* แบบอะตอมมิก
    Idempotent: ถ้า final ครบอยู่แล้ว จะลบ tmp แล้วคืนค่าเดิม
    """
    final = catalog_paths_for_job(model, job_name, ext=".gcode")

    # ถ้าปลายทางครบแล้ว -> เก็บกวาด tmp แล้วคืนเลย
    if is_triple_complete(final):
        for k in ("gcode_tmp", "json_tmp", "preview_tmp"):
            if object_exists(tmp[k]):
                delete_object(tmp[k])
        return final

    # ต้องครบสามใน staging ก่อนเท่านั้น
    for k in ("gcode_tmp", "json_tmp", "preview_tmp"):
        if not object_exists(tmp[k]):
            raise RuntimeError(f"TRIPLE_INCOMPLETE: missing {k}")

    _move_object(tmp["gcode_tmp"],   final["gcode"])
    _move_object(tmp["json_tmp"],    final["json"])
    _move_object(tmp["preview_tmp"], final["preview"])

    return final

# ==== Presign helpers ====
def presign_put(object_key: str, content_type: Optional[str] = None, size: Optional[int] = None) -> Dict[str, Any]:
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
    url = _rewrite_presign_host(url)
    return {
        "object_key": object_key,
        "url": url,
        "method": "PUT",
        "headers": headers,
        "expires_in": PRESIGN_SEC,
    }

def presign_get(object_key: str) -> str:
    _validate_key(object_key)
    url = _s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": S3_BUCKET, "Key": object_key},
        ExpiresIn=PRESIGN_SEC,
    )
    return _rewrite_presign_host(url)

# ==== Basic ops ====
def head_object(object_key: str) -> Dict[str, Any]:
    _validate_key(object_key)
    return _s3.head_object(Bucket=S3_BUCKET, Key=object_key)

def delete_object(object_key: str) -> None:
    _validate_key(object_key)
    _s3.delete_object(Bucket=S3_BUCKET, Key=object_key)

def copy_object(
    src_key: str,
    dst_key: str,
    content_type: Optional[str] = None,
    cache_control: Optional[str] = None,
    metadata: Optional[Dict[str, str]] = None,
) -> None:
    _validate_key(src_key)
    _validate_key(dst_key)
    kwargs: Dict[str, Any] = {
        "Bucket": S3_BUCKET,
        "Key": dst_key,
        "CopySource": {"Bucket": S3_BUCKET, "Key": src_key},
    }
    if content_type or metadata or cache_control:
        kwargs["MetadataDirective"] = "REPLACE"
        if content_type:
            kwargs["ContentType"] = content_type
        if metadata:
            kwargs["Metadata"] = metadata
        if cache_control:
            kwargs["CacheControl"] = cache_control
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
    cache_control: Optional[str] = None,
) -> Dict[str, Any]:
    _validate_key(object_key)
    ct = content_type or _guess_content_type(object_key)
    kwargs: Dict[str, Any] = {
        "Bucket": S3_BUCKET,
        "Key": object_key,
        "Body": data,
        "ContentType": ct,
    }
    if metadata:
        kwargs["Metadata"] = metadata
    if cache_control:
        kwargs["CacheControl"] = cache_control
    resp = _s3.put_object(**kwargs)
    return {"object_key": object_key, "etag": resp.get("ETag")}

def put_object(
    object_key: str,
    data: bytes,
    content_type: Optional[str] = None,
    metadata: Optional[Dict[str, str]] = None,
    cache_control: Optional[str] = None,
) -> Dict[str, Any]:
    return upload_bytes(
        data=data,
        object_key=object_key,
        content_type=content_type,
        metadata=metadata,
        cache_control=cache_control,
    )

# ==== Streaming ====
def open_object_stream(object_key: str) -> Tuple[Iterable[bytes], Optional[int], Optional[str]]:
    _validate_key(object_key)
    try:
        stat = _s3.head_object(Bucket=S3_BUCKET, Key=object_key)
    except ClientError as e:
        code = (e.response or {}).get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            raise FileNotFoundError(object_key)
        raise

    try:
        resp = _s3.get_object(Bucket=S3_BUCKET, Key=object_key)
        body = resp["Body"]
    except ClientError as e:
        code = (e.response or {}).get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            raise FileNotFoundError(object_key)
        raise

    def _iter() -> Iterable[bytes]:
        try:
            if hasattr(body, "iter_chunks"):
                for chunk in body.iter_chunks(chunk_size=256 * 1024):
                    if chunk:
                        yield chunk
            else:
                while True:
                    chunk = body.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                body.close()
            except Exception:
                pass

    size = stat.get("ContentLength")
    ct = (stat.get("ContentType") or None) or _guess_content_type(object_key)
    return _iter(), int(size) if size is not None else None, ct

# ==== Partial object reader ====
def get_object_range(object_key: str, start: int = 0, length: int = 128 * 1024) -> bytes:
    _validate_key(object_key)
    start = max(0, int(start))
    length = max(1, int(length))
    end = start + length - 1
    try:
        resp = _s3.get_object(
            Bucket=S3_BUCKET,
            Key=object_key,
            Range=f"bytes={start}-{end}",
        )
        return resp["Body"].read()
    except ClientError as e:
        code = (e.response or {}).get("Error", {}).get("Code", "")
        if code in ("InvalidRange", "416"):
            return b""
        if code in ("404", "NoSuchKey", "NotFound"):
            raise FileNotFoundError(object_key)
        raise

# ==== Prefix utilities ====
def list_objects(Prefix: str, MaxKeys: int = 1000) -> List[Dict[str, Any]]:
    prefix = normalize_s3_prefix(Prefix, S3_BUCKET)
    out: List[Dict[str, Any]] = []
    token: Optional[str] = None
    while True:
        kw = {"Bucket": S3_BUCKET, "Prefix": prefix, "MaxKeys": MaxKeys}
        if token:
            kw["ContinuationToken"] = token
        resp = _s3.list_objects_v2(**kw)
        contents = resp.get("Contents") or []
        for it in contents:
            out.append(
                {
                    "Key": it["Key"],
                    "Size": int(it.get("Size") or 0),
                    "ETag": it.get("ETag"),
                    "LastModified": it.get("LastModified").isoformat() if it.get("LastModified") else None,
                }
            )
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
        else:
            break
    return out

def delete_objects(keys: Iterable[str]) -> int:
    ks = [k for k in keys if k]
    if not ks:
        return 0
    total = 0
    for i in range(0, len(ks), 1000):
        chunk = ks[i : i + 1000]
        _s3.delete_objects(
            Bucket=S3_BUCKET,
            Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True},
        )
        total += len(chunk)
    return total

def move_prefix(src_prefix: str, dst_prefix: str) -> int:
    sp = normalize_s3_prefix(src_prefix, S3_BUCKET)
    dp = normalize_s3_prefix(dst_prefix, S3_BUCKET)
    objs = list_objects(Prefix=sp)
    if not objs:
        return 0
    for o in objs:
        src = o["Key"]
        dst = dp + src[len(sp):]
        copy_object(src_key=src, dst_key=dst)
    delete_objects([o["Key"] for o in objs])
    return len(objs)

def ensure_visible_prefix(prefix: str) -> str:
    p = normalize_s3_prefix(prefix, S3_BUCKET)
    keep_key = p + "__KEEP__"
    try:
        head_object(keep_key)
        return keep_key
    except Exception:
        put_object(keep_key, b"", content_type="application/octet-stream")
        return keep_key