from functools import lru_cache
from pathlib import Path
from typing import BinaryIO

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.config import get_settings


class ObjectStorage:
    """Thin S3/MinIO wrapper; every transfer is streamed, never buffered in memory."""

    def __init__(self, bucket: str, client: object | None = None) -> None:
        self.bucket = bucket
        self._client = client or _build_client()

    def ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self.bucket)

    def upload_fileobj(self, key: str, fileobj: BinaryIO, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else None
        self._client.upload_fileobj(fileobj, self.bucket, key, ExtraArgs=extra)

    def upload_file(self, key: str, path: Path, content_type: str | None = None) -> None:
        with path.open("rb") as handle:
            self.upload_fileobj(key, handle, content_type)

    def download_file(self, key: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(self.bucket, key, str(path))

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def delete_prefix(self, prefix: str) -> None:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys = [{"Key": item["Key"]} for item in page.get("Contents", [])]
            if keys:
                self._client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})


def _build_client() -> object:
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


@lru_cache(maxsize=1)
def get_storage() -> ObjectStorage:
    return ObjectStorage(get_settings().s3_bucket)


def raw_key(job_id: str, media_format: str) -> str:
    return f"jobs/{job_id}/raw.{media_format}"


def chunk_key(job_id: str, index: int) -> str:
    return f"jobs/{job_id}/chunks/{index:05d}.wav"


def chunk_prefix(job_id: str) -> str:
    return f"jobs/{job_id}/chunks/"


def job_prefix(job_id: str) -> str:
    return f"jobs/{job_id}/"
