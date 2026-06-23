"""ObjectStore round-trip + presigned-URL test against a throwaway MinIO.

SKIPs cleanly unless ``KINORA_TEST_S3_ENDPOINT_URL`` is set.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from app.storage.object_store import ObjectStore, keys

_ENDPOINT = os.environ.get("KINORA_TEST_S3_ENDPOINT_URL")

pytestmark = pytest.mark.skipif(
    not _ENDPOINT, reason="KINORA_TEST_S3_ENDPOINT_URL not set; skipping object-store test"
)


def _store() -> ObjectStore:
    return ObjectStore(
        endpoint_url=os.environ["KINORA_TEST_S3_ENDPOINT_URL"],
        region=os.environ.get("KINORA_TEST_S3_REGION", "us-east-1"),
        access_key=os.environ.get("KINORA_TEST_S3_ACCESS_KEY", "kinora"),
        secret_key=os.environ.get("KINORA_TEST_S3_SECRET_KEY", "kinora-secret"),
        bucket=os.environ.get("KINORA_TEST_S3_BUCKET", "kinora-test"),
    )


def test_object_store_roundtrip_and_presign() -> None:
    store = _store()
    store.ensure_bucket()
    # Idempotent: a second ensure_bucket must not error.
    store.ensure_bucket()

    key = keys.clip("book_demo", f"shot_{uuid.uuid4().hex[:8]}")
    payload = b"\x00\x01kinora-clip-bytes\xff"

    assert store.exists(key) is False
    store.put_bytes(key, payload, content_type="video/mp4")
    assert store.exists(key) is True
    assert store.get_bytes(key) == payload

    # Presigned GET returns the bytes over HTTP.
    get_url = store.presigned_get_url(key, ttl=120)
    response = httpx.get(get_url, timeout=10.0)
    assert response.status_code == 200
    assert response.content == payload

    # Presigned PUT uploads, then the object is readable.
    put_key = keys.keyframe("book_demo", "beat_0001")
    put_url = store.presigned_put_url(put_key, ttl=120)
    upload = httpx.put(put_url, content=b"keyframe-png-bytes", timeout=10.0)
    assert upload.status_code in (200, 204)
    assert store.get_bytes(put_key) == b"keyframe-png-bytes"

    # Cleanup.
    store.delete(key)
    store.delete(put_key)
    assert store.exists(key) is False


def test_key_builders_layout() -> None:
    assert keys.clip("b", "s") == "clips/b/s.mp4"
    assert keys.keyframe("b", "beat_1") == "keyframes/b/beat_1.png"
    assert keys.audio("b", "s") == "audio/b/s.wav"
    assert keys.ref("b", "char_elsa", "ref_front.png") == "refs/b/char_elsa/ref_front.png"
    assert keys.lastframe("b", "s") == "lastframes/b/s.png"
    assert keys.pdf("b") == "pdfs/b.pdf"
    assert keys.page_image("b", 3) == "pages/b/0003.png"
    assert keys.canon("b", "elsa.md") == "canon/b/elsa.md"
