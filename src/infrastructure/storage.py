"""Object storage (FR-20-01) — ONE boto3 S3-client code path for both environments; only the
endpoint/credentials differ (config/env_config.py: `s3_endpoint_url` points at a local MinIO
instance in dev/test, is unset -> real AWS S3 in prod). Callers depend on `put_object` /
`get_presigned_url`, never on a concrete provider — same "one interface, one factory" posture as
`src.infrastructure.emailer`.
"""
from typing import Any

import boto3
from botocore.client import Config

from config.env_config import settings


def _client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4"),
    )


def ensure_bucket() -> None:
    """Idempotent create. Dev/test convenience (MinIO starts with no buckets); a real prod bucket
    is provisioned out-of-band, so this is a no-op there (head_bucket succeeds, nothing changes)."""
    client = _client()
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
    except Exception:  # noqa: BLE001 - any failure to find it means "doesn't exist yet", create it
        client.create_bucket(Bucket=settings.s3_bucket)


def put_object(key: str, body: bytes, content_type: str = "application/json") -> None:
    ensure_bucket()
    _client().put_object(Bucket=settings.s3_bucket, Key=key, Body=body, ContentType=content_type)


def get_presigned_url(key: str, expires_in: int = 3600) -> str:
    url: str = _client().generate_presigned_url(
        "get_object", Params={"Bucket": settings.s3_bucket, "Key": key}, ExpiresIn=expires_in
    )
    return url
