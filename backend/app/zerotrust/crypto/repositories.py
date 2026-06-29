"""Async DB repositories bridging the in-memory primitives to the ``crypto_*`` tables.

These wrap an :class:`~sqlalchemy.ext.asyncio.AsyncSession` and follow the
project convention: flush (to surface constraint errors / populate defaults) but
never commit — the unit-of-work boundary owns the transaction.

* :class:`TokenVaultRepo` — durable token vault: persist/lookup token rows and
  append detokenize-audit entries. The wrapped DEK is stored as JSON
  (``kek_id``/``kek_version``/``algorithm``/``wrapped`` hex) so a KEK rotation can
  rewrite only that column.
* :class:`BlindIndexRepo` — write a cell's blind-index tokens and resolve a query
  probe token to the owning row ids.
* :class:`KekRegistryRepo` — keep :class:`CryptoKekRegistry` in sync with KMS KEK
  versions / lifecycle (the queryable catalogue; material stays in the KMS).
* :class:`RotationJobRepo` — create/advance/finish the durable rotation cursor.

The token-vault repo also adapts to the synchronous
:class:`~app.zerotrust.crypto.tokenization.TokenStore` Protocol via
:class:`AsyncTokenStoreAdapter` for callers that already hold loaded rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.zerotrust.crypto.aead import Algorithm
from app.zerotrust.crypto.context import Ciphertext
from app.zerotrust.crypto.keys import WrappedDek
from app.zerotrust.crypto.models import (
    BlindIndexKind,
    CryptoBlindIndex,
    CryptoKekRegistry,
    CryptoRotationJob,
    CryptoTokenAccessLog,
    CryptoTokenVault,
    KekLifecycle,
    RotationMode,
    RotationStatus,
    TokenSchemeEnum,
)
from app.zerotrust.crypto.tokenization import (
    DetokenizationRequest,
    TokenPolicy,
    TokenRecord,
    TokenScheme,
)


def _wrapped_to_json(w: WrappedDek) -> dict[str, Any]:
    return {
        "kek_id": w.kek_id,
        "kek_version": w.kek_version,
        "algorithm": int(w.algorithm),
        "wrapped": w.ciphertext.hex(),
    }


def _wrapped_from_json(d: dict[str, Any]) -> WrappedDek:
    return WrappedDek(
        kek_id=d["kek_id"],
        kek_version=int(d["kek_version"]),
        ciphertext=bytes.fromhex(d["wrapped"]),
        algorithm=Algorithm(int(d["algorithm"])),
    )


class TokenVaultRepo:
    """Durable token vault + detokenize audit."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def put(
        self,
        token: str,
        ciphertext: Ciphertext,
        policy: TokenPolicy,
        scheme: TokenScheme,
        *,
        plaintext_id: str | None = None,
    ) -> CryptoTokenVault:
        row = CryptoTokenVault(
            token=token,
            plaintext_id=plaintext_id,
            envelope=ciphertext.envelope,
            wrapped_dek=_wrapped_to_json(ciphertext.wrapped_dek),
            scheme=TokenSchemeEnum(scheme.value),
            allowed_purposes=sorted(policy.purposes),
            data_class=policy.data_class,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, token: str) -> TokenRecord | None:
        row = (
            await self.session.execute(
                select(CryptoTokenVault).where(CryptoTokenVault.token == token)
            )
        ).scalar_one_or_none()
        return self._to_record(row) if row else None

    async def get_by_plaintext_id(self, plaintext_id: str) -> TokenRecord | None:
        row = (
            await self.session.execute(
                select(CryptoTokenVault).where(
                    CryptoTokenVault.plaintext_id == plaintext_id
                )
            )
        ).scalar_one_or_none()
        return self._to_record(row) if row else None

    async def exists(self, token: str) -> bool:
        return (
            await self.session.execute(
                select(CryptoTokenVault.id).where(CryptoTokenVault.token == token)
            )
        ).first() is not None

    async def log_access(
        self,
        request: DetokenizationRequest,
        token: str,
        *,
        allowed: bool,
        detail: str | None = None,
    ) -> None:
        self.session.add(
            CryptoTokenAccessLog(
                token=token,
                actor=request.actor,
                purpose=request.purpose,
                allowed=allowed,
                detail=detail,
            )
        )
        await self.session.flush()

    @staticmethod
    def _to_record(row: CryptoTokenVault) -> TokenRecord:
        return TokenRecord(
            token=row.token,
            ciphertext=Ciphertext(
                envelope=bytes(row.envelope),
                wrapped_dek=_wrapped_from_json(row.wrapped_dek),
            ),
            policy=TokenPolicy(
                purposes=frozenset(row.allowed_purposes), data_class=row.data_class
            ),
            scheme=TokenScheme(row.scheme.value),
        )


class BlindIndexRepo:
    """Write/query the keyed blind-index companion table."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def replace_for_cell(
        self,
        *,
        source_table: str,
        source_column: str,
        source_row_id: str,
        equality: bytes | None = None,
        prefixes: tuple[bytes, ...] = (),
        range_bucket: bytes | None = None,
    ) -> int:
        """Replace all index rows for one cell (idempotent re-index on update)."""
        from sqlalchemy import delete

        await self.session.execute(
            delete(CryptoBlindIndex).where(
                CryptoBlindIndex.source_table == source_table,
                CryptoBlindIndex.source_column == source_column,
                CryptoBlindIndex.source_row_id == source_row_id,
            )
        )
        written = 0
        if equality is not None:
            self.session.add(
                self._row(
                    source_table, source_column, source_row_id,
                    BlindIndexKind.EQUALITY, equality,
                )
            )
            written += 1
        for tok in prefixes:
            self.session.add(
                self._row(
                    source_table, source_column, source_row_id,
                    BlindIndexKind.PREFIX, tok,
                )
            )
            written += 1
        if range_bucket is not None:
            self.session.add(
                self._row(
                    source_table, source_column, source_row_id,
                    BlindIndexKind.RANGE, range_bucket,
                )
            )
            written += 1
        await self.session.flush()
        return written

    async def find_rows(
        self, *, source_table: str, source_column: str, kind: BlindIndexKind, token: bytes
    ) -> list[str]:
        """Return the row ids whose ``kind`` token equals ``token`` (the search)."""
        rows = (
            await self.session.execute(
                select(CryptoBlindIndex.source_row_id).where(
                    CryptoBlindIndex.source_table == source_table,
                    CryptoBlindIndex.source_column == source_column,
                    CryptoBlindIndex.kind == kind,
                    CryptoBlindIndex.token == token,
                )
            )
        ).scalars().all()
        return list(rows)

    @staticmethod
    def _row(
        table: str, column: str, row_id: str, kind: BlindIndexKind, token: bytes
    ) -> CryptoBlindIndex:
        return CryptoBlindIndex(
            source_table=table,
            source_column=column,
            source_row_id=row_id,
            kind=kind,
            token=token,
        )


class KekRegistryRepo:
    """Mirror KMS KEK versions/lifecycle into the queryable catalogue."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_version(
        self,
        kek_id: str,
        version: int,
        state: KekLifecycle,
        *,
        is_active: bool,
        provenance: dict[str, Any] | None = None,
    ) -> CryptoKekRegistry:
        existing = (
            await self.session.execute(
                select(CryptoKekRegistry).where(
                    CryptoKekRegistry.kek_id == kek_id,
                    CryptoKekRegistry.version == version,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.state = state
            existing.is_active = is_active
            if provenance is not None:
                existing.provenance = provenance
            await self.session.flush()
            return existing
        row = CryptoKekRegistry(
            kek_id=kek_id,
            version=version,
            state=state,
            is_active=is_active,
            provenance=provenance,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def deactivate_others(self, kek_id: str, active_version: int) -> None:
        """Clear ``is_active`` on every version of ``kek_id`` except ``active_version``."""
        rows = (
            await self.session.execute(
                select(CryptoKekRegistry).where(CryptoKekRegistry.kek_id == kek_id)
            )
        ).scalars().all()
        for row in rows:
            row.is_active = row.version == active_version
        await self.session.flush()


class RotationJobRepo:
    """Create + advance the durable rotation cursor/ledger."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        mode: RotationMode,
        source_table: str,
        source_column: str,
        kek_id: str,
        target_version: int | None = None,
    ) -> CryptoRotationJob:
        job = CryptoRotationJob(
            mode=mode,
            status=RotationStatus.PENDING,
            source_table=source_table,
            source_column=source_column,
            kek_id=kek_id,
            target_version=target_version,
            scanned=0,
            rotated=0,
            skipped=0,
        )
        self.session.add(job)
        await self.session.flush()
        return job

    async def mark_running(self, job: CryptoRotationJob) -> None:
        job.status = RotationStatus.RUNNING
        if job.started_at is None:
            job.started_at = datetime.now(UTC)
        await self.session.flush()

    async def advance(
        self,
        job: CryptoRotationJob,
        *,
        scanned: int,
        rotated: int,
        skipped: int,
        cursor: str | None,
    ) -> None:
        job.scanned += scanned
        job.rotated += rotated
        job.skipped += skipped
        job.cursor = cursor
        await self.session.flush()

    async def finish(
        self, job: CryptoRotationJob, *, status: RotationStatus, error: str | None = None
    ) -> None:
        job.status = status
        job.finished_at = datetime.now(UTC)
        job.last_error = error
        await self.session.flush()


__all__ = [
    "BlindIndexRepo",
    "KekRegistryRepo",
    "RotationJobRepo",
    "TokenVaultRepo",
]
