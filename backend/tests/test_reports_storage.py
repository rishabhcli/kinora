"""Unit tests for report artifact storage + signed retrieval (fake object store)."""

from __future__ import annotations

from app.reports.render import ReportFormat
from app.reports.storage import ReportArtifactStore, content_hash, report_key


class FakeObjectStore:
    """An in-memory stand-in for :class:`ObjectStore` (no infra)."""

    def __init__(self, public_base: str | None = "https://cdn.example/kinora") -> None:
        self.objects: dict[str, tuple[bytes, str | None]] = {}
        self._public = public_base

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.objects[key] = (data, content_type)

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key][0]

    def exists(self, key: str) -> bool:
        return key in self.objects

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def presigned_get_url(self, key: str, ttl: int = 3600) -> str:
        if self._public:
            return f"{self._public}/{key}"
        return f"https://signed.example/{key}?ttl={ttl}"


def test_content_hash_is_sha256_hex() -> None:
    h = content_hash(b"hello")
    assert len(h) == 64
    assert h == content_hash(b"hello")
    assert h != content_hash(b"world")


def test_report_key_is_owner_scoped_and_content_addressed() -> None:
    digest = "a" * 64
    key = report_key(owner_id="u1", kind="reading_progress", digest=digest, fmt=ReportFormat.PDF)
    assert key == "reports/u1/reading_progress/aaaaaaaaaaaaaaaa.pdf"
    anon = report_key(owner_id=None, kind="budget", digest=digest, fmt=ReportFormat.CSV)
    assert anon == "reports/_/budget/aaaaaaaaaaaaaaaa.csv"


def test_put_returns_key_and_hash_and_stores_bytes() -> None:
    fake = FakeObjectStore()
    store = ReportArtifactStore(fake)
    data = b"%PDF-1.7 ..."
    key, digest = store.put(data, owner_id="u1", kind="budget", fmt=ReportFormat.PDF)
    assert digest == content_hash(data)
    assert fake.objects[key][0] == data
    assert fake.objects[key][1] == "application/pdf"


def test_put_is_content_addressed_idempotent() -> None:
    fake = FakeObjectStore()
    store = ReportArtifactStore(fake)
    data = b"same bytes"
    k1, h1 = store.put(data, owner_id="u1", kind="budget", fmt=ReportFormat.JSON)
    k2, h2 = store.put(data, owner_id="u1", kind="budget", fmt=ReportFormat.JSON)
    assert (k1, h1) == (k2, h2)
    assert len(fake.objects) == 1


def test_signed_url_and_fetch_and_delete() -> None:
    fake = FakeObjectStore()
    store = ReportArtifactStore(fake)
    key, _ = store.put(b"x", owner_id=None, kind="quality", fmt=ReportFormat.HTML)
    assert store.signed_url(key).endswith(key)
    assert store.fetch(key) == b"x"
    assert store.exists(key)
    store.delete(key)
    assert not store.exists(key)
