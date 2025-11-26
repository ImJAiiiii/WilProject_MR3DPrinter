import boto3, botocore, os
endpoint = "http://localhost:9000/"
bucket   = "printer-store"   # ???????? S3_BUCKET ?? .env
access   = "admin"           # ???????? S3_ACCESS_KEY
secret   = "admin123"        # ???????? S3_SECRET_KEY

s3 = boto3.client(
    "s3",
    endpoint_url=endpoint,
    aws_access_key_id=access,
    aws_secret_access_key=secret,
    region_name="us-east-1",
    config=boto3.session.Config(signature_version="s3v4", s3={"addressing_style":"path"})
)

try:
    s3.head_bucket(Bucket=bucket)
    print("Bucket exists:", bucket)
except botocore.exceptions.ClientError as e:
    code = e.response.get("Error",{}).get("Code")
    if code in ("404","NoSuchBucket"):
        s3.create_bucket(Bucket=bucket)
        print("Bucket created:", bucket)
    else:
        raise
