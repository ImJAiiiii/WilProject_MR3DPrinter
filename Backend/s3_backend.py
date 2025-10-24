# s3_backend.py
from fastapi import FastAPI, HTTPException, Body
from pydantic import BaseModel
import os, boto3
from botocore.client import Config

S3_ENDPOINT = os.getenv("S3_ENDPOINT", "http://localhost:9000")
S3_REGION   = os.getenv("S3_REGION", "us-east-1")
S3_ACCESS   = os.getenv("S3_ACCESS_KEY", "minioadmin")
S3_SECRET   = os.getenv("S3_SECRET_KEY", "minioadmin")
S3_BUCKET   = os.getenv("S3_BUCKET", "printer-store")
USE_HTTPS   = os.getenv("S3_SECURE", "false").lower() == "true"

session = boto3.session.Session()
s3 = session.client(
    "s3",
    endpoint_url=S3_ENDPOINT,
    region_name=S3_REGION,
    aws_access_key_id=S3_ACCESS,
    aws_secret_access_key=S3_SECRET,
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    use_ssl=USE_HTTPS,
)

app = FastAPI()

class UploadReq(BaseModel):
    key: str                 # เช่น "staging/machineA/user42/part123.gcode"
    content_type: str = "application/octet-stream"

@app.get("/s3/list")
def list_store(prefix: str = "staging/", limit: int = 100):
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix, MaxKeys=limit)
    items = []
    for it in resp.get("Contents", []):
        items.append({
            "key": it["Key"],
            "size": it["Size"],
            "last_modified": it["LastModified"].isoformat()
        })
    return {"items": items}

@app.post("/s3/presign-upload")
def presign_upload(req: UploadReq):
    url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": req.key, "ContentType": req.content_type},
        ExpiresIn=60 * 10,  # 10 นาที
    )
    return {"url": url}

@app.get("/s3/presign-download")
def presign_download(key: str):
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=60 * 10,
    )
    return {"url": url}
