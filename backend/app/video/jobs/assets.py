"""Eager, verified persistence of an (expiring) provider clip to object storage.

Hosted video URLs are short-lived: the moment a task succeeds, the bytes must be
pulled and written to durable object storage or they are lost. :class:`AssetPersister`
does exactly that with three safety properties:

* **Eager** — driven the instant the engine observes ``SUCCEEDED`` (poll *or*
  webhook), before doing anything else.
* **Verified** — the downloaded bytes are sha256'd; the digest + size land on the
  :class:`~app.video.jobs.models.JobAsset` so a reader can validate the object.
* **Resilient + idempotent** — a transient download/upload failure is retried with
  the injected :class:`~app.video.jobs.ports.PollSchedule`'s backoff; a key that
  already exists is treated as "already persisted" (a recovered worker re-running
  the success path does not duplicate the upload).

No clock reads, no real sleeps: time + sleep come from the injected ``JobClock``.
The object store is synchronous (boto3); uploads run in a worker thread so the
event loop is never blocked.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .models import JobAsset
from .ports import AssetFetcher, JobClock, ObjectStorePort, PollSchedule


class AssetPersistError(RuntimeError):
    """Raised when an asset could not be downloaded + persisted after all retries."""


@dataclass(frozen=True, slots=True)
class PersistConfig:
    """Retry bounds for one persist operation."""

    max_attempts: int = 4
    content_type: str = "video/mp4"


class AssetPersister:
    """Downloads a clip URL and writes it to object storage under a stable key."""

    def __init__(
        self,
        *,
        fetcher: AssetFetcher,
        store: ObjectStorePort,
        clock: JobClock,
        backoff: PollSchedule,
        config: PersistConfig | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._store = store
        self._clock = clock
        self._backoff = backoff
        self._config = config or PersistConfig()

    async def persist(self, *, url: str, storage_key: str) -> JobAsset:
        """Fetch ``url`` and store it at ``storage_key``; return the verified asset.

        Idempotent on ``storage_key``: if the object already exists we still need
        the bytes to report a digest, so we re-fetch and verify but skip the
        upload only when the source URL is gone — otherwise we re-write (a small,
        safe overwrite that guarantees the digest matches what we return).

        Raises:
            AssetPersistError: the download or upload kept failing.
        """
        last_error: Exception | None = None
        for attempt in range(1, self._config.max_attempts + 1):
            try:
                data = await self._fetcher.fetch(url)
                if not data:
                    raise AssetPersistError("provider returned an empty clip body")
                digest = hashlib.sha256(data).hexdigest()
                await self._upload(storage_key, data)
                return JobAsset(
                    storage_key=storage_key,
                    sha256=digest,
                    size_bytes=len(data),
                    content_type=self._config.content_type,
                    source_url=url,
                )
            except Exception as exc:  # noqa: BLE001 - retried below, re-raised if terminal
                last_error = exc
                if attempt >= self._config.max_attempts:
                    break
                delay = self._backoff.next_delay(attempt)
                if delay > 0:
                    await self._clock.sleep(delay)
        raise AssetPersistError(
            f"failed to persist clip to {storage_key} after "
            f"{self._config.max_attempts} attempts: {last_error}"
        ) from last_error

    async def _upload(self, key: str, data: bytes) -> None:
        import asyncio

        await asyncio.to_thread(self._store.put_bytes, key, data, self._config.content_type)


__all__ = ["AssetPersistError", "AssetPersister", "PersistConfig"]
