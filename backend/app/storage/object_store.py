"""S3-compatible object store client (boto3) with signed URLs and key helpers."""

from __future__ import annotations

from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import Settings, get_settings

# Object-storage codes that mean "not found" across S3/MinIO for head requests.
_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NoSuchBucket", "NotFound"})


class Keys:
    """Builders for the canonical object-key layout (all paths are key prefixes)."""

    @staticmethod
    def clip(book_id: str, shot_id: str) -> str:
        """Rendered video clip for a shot."""
        return f"clips/{book_id}/{shot_id}.mp4"

    @staticmethod
    def keyframe(book_id: str, beat_id: str) -> str:
        """Speculative keyframe still for a beat."""
        return f"keyframes/{book_id}/{beat_id}.png"

    @staticmethod
    def audio(book_id: str, shot_id: str) -> str:
        """Narration audio for a shot."""
        return f"audio/{book_id}/{shot_id}.wav"

    @staticmethod
    def ref(book_id: str, entity_key: str, name: str) -> str:
        """Locked reference asset (image/audio) for a canon entity."""
        return f"refs/{book_id}/{entity_key}/{name}"

    @staticmethod
    def lastframe(book_id: str, shot_id: str) -> str:
        """Last frame of a shot (continuation/endpoint for the next shot)."""
        return f"lastframes/{book_id}/{shot_id}.png"

    @staticmethod
    def pdf(book_id: str) -> str:
        """The uploaded source PDF."""
        return f"pdfs/{book_id}.pdf"

    @staticmethod
    def canon(book_id: str, name: str) -> str:
        """Canon markdown-vault export artifact."""
        return f"canon/{book_id}/{name}"


# Convenient lowercase alias: ``keys.clip(...)`` etc.
keys = Keys


class ObjectStore:
    """A thin, typed boto3 wrapper for one bucket.

    Path-style addressing and Signature V4 are forced so the same client works
    against MinIO locally and S3/OSS in production.
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        region: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        public_base_url: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._public_base_url = public_base_url.rstrip("/") if public_base_url else None
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(
                s3={"addressing_style": "path"},
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> ObjectStore:
        """Build an :class:`ObjectStore` from application :class:`Settings`."""
        s = settings or get_settings()
        return cls(
            endpoint_url=s.s3_endpoint_url,
            region=s.s3_region,
            access_key=s.s3_access_key,
            secret_key=s.s3_secret_key,
            bucket=s.s3_bucket,
            public_base_url=s.s3_public_base_url,
        )

    @property
    def bucket(self) -> str:
        """The bucket this client is bound to."""
        return self._bucket

    def ensure_bucket(self) -> None:
        """Create the bucket if it does not already exist (idempotent)."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in _NOT_FOUND_CODES:
                self._client.create_bucket(Bucket=self._bucket)
            else:
                raise

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        """Upload raw bytes to ``key``."""
        extra: dict[str, Any] = {}
        if content_type is not None:
            extra["ContentType"] = content_type
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, **extra)

    def put_file(self, key: str, path: str, content_type: str | None = None) -> None:
        """Upload a local file at ``path`` to ``key``."""
        extra_args: dict[str, Any] | None = (
            {"ContentType": content_type} if content_type is not None else None
        )
        self._client.upload_file(Filename=path, Bucket=self._bucket, Key=key, ExtraArgs=extra_args)

    def get_bytes(self, key: str) -> bytes:
        """Download the object at ``key`` as bytes."""
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        body: bytes = response["Body"].read()
        return body

    def exists(self, key: str) -> bool:
        """Return whether an object exists at ``key``."""
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in _NOT_FOUND_CODES:
                return False
            raise
        return True

    def delete(self, key: str) -> None:
        """Delete the object at ``key`` (no error if it is already absent)."""
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def presigned_get_url(self, key: str, ttl: int = 3600) -> str:
        """Return a time-limited URL that downloads ``key``."""
        url: str = self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=ttl,
        )
        return url

    def presigned_put_url(
        self, key: str, ttl: int = 3600, content_type: str | None = None
    ) -> str:
        """Return a time-limited URL that uploads to ``key``."""
        params: dict[str, Any] = {"Bucket": self._bucket, "Key": key}
        if content_type is not None:
            params["ContentType"] = content_type
        url: str = self._client.generate_presigned_url(
            "put_object", Params=params, ExpiresIn=ttl
        )
        return url

    def public_url(self, key: str) -> str | None:
        """Return a stable public URL for ``key`` if a public base URL is configured."""
        if self._public_base_url is None:
            return None
        return f"{self._public_base_url}/{key}"
