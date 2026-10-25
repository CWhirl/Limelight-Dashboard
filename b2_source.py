"""Read-only Backblaze B2 S3-compatible object enumeration and retrieval."""

from __future__ import annotations

from datetime import datetime, timezone

import boto3
from botocore.config import Config

from limelight_parser import ALLOWED_EXTENSIONS


def connect(endpoint: str, key_id: str, application_key: str):
    if not endpoint.startswith("https://s3.") or not endpoint.endswith(".backblazeb2.com"):
        raise ValueError("Use your bucket's HTTPS B2 S3 endpoint, e.g. https://s3.us-west-004.backblazeb2.com")
    region = endpoint.removeprefix("https://s3.").removesuffix(".backblazeb2.com")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=key_id,
        aws_secret_access_key=application_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                      connect_timeout=8, read_timeout=30,
                      retries={"max_attempts": 3, "mode": "standard"}),
    )


def list_log_objects(client, bucket: str, prefix: str, scan_limit: int = 10000) -> tuple[list[dict], bool]:
    """List at most scan_limit keys, sort matching log objects by modification date."""
    candidates = []
    seen = 0
    truncated = False
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, PaginationConfig={"PageSize": 1000}):
        for entry in page.get("Contents", []):
            seen += 1
            key = entry["Key"]
            if key.lower().endswith(ALLOWED_EXTENSIONS) and not key.endswith("/"):
                candidates.append({"key": key, "size": int(entry["Size"]), "modified": entry["LastModified"]})
            if seen >= scan_limit:
                truncated = True
                break
        if truncated:
            break
    candidates.sort(key=lambda d: d["modified"], reverse=True)
    return candidates, truncated


def read_object(client, bucket: str, key: str, max_size: int) -> bytes:
    """Never download more than max_size + 1 bytes."""
    resp = client.get_object(Bucket=bucket, Key=key)
    try:
        return resp["Body"].read(max_size + 1)
    finally:
        resp["Body"].close()
