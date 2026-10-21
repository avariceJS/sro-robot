"""Durable transport manifests for recovering multipart uploads across staff browsers."""

from __future__ import annotations

import hashlib
import json
import re
import secrets

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from ..db.models import (
    UploadTransportBatch,
    UploadTransportBatchItem,
    UploadTransportChunkReceipt,
)


class UploadTransportConflict(ValueError):
    """An idempotency key or immutable chunk plan was reused with different input."""


class UploadTransportIncomplete(ValueError):
    """An operation requires a batch that has not received every planned chunk."""


def manifest_sha256(files: list[dict], files_per_chunk: int) -> str:
    canonical = json.dumps(
        {"version": 1, "files_per_chunk": files_per_chunk, "files": files},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(b"sro-robot/upload-transport-manifest/v1\0" + canonical).hexdigest()


def request_key_sha256(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{48}", value) is None:
        raise ValueError("Upload batch registration key must be lowercase hexadecimal")
    return hashlib.sha256(b"sro-robot/upload-transport-request-key/v1\0" + bytes.fromhex(value)).hexdigest()


def reprocess_key_sha256(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{48}", value) is None:
        raise ValueError("Upload batch reprocessing key must be lowercase hexadecimal")
    return hashlib.sha256(
        b"sro-robot/upload-transport-reprocess-key/v1\0" + bytes.fromhex(value)
    ).hexdigest()


def register_batch(
    db: Session,
    *,
    creator_id: int,
    request_key: str,
    files_per_chunk: int,
    files: list[dict],
) -> tuple[UploadTransportBatch, bool]:
    """Insert one immutable complete manifest or return an exact registration replay."""
    if db.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Shared upload batches require PostgreSQL")
    key_hash = request_key_sha256(request_key)
    digest = manifest_sha256(files, files_per_chunk)
    batch_id = secrets.token_hex(16)
    statement = (
        postgresql_insert(UploadTransportBatch)
        .values(
            id=batch_id,
            created_by_id=creator_id,
            request_key_sha256=key_hash,
            manifest_sha256=digest,
            files_per_chunk=files_per_chunk,
            file_count=len(files),
            total_bytes=sum(file["size"] for file in files),
        )
        .on_conflict_do_nothing(constraint="uq_upload_transport_batch_creator_key")
        .returning(UploadTransportBatch.id)
    )
    inserted_id = db.scalar(statement)
    if inserted_id is None:
        batch = db.scalar(
            select(UploadTransportBatch)
            .where(
                UploadTransportBatch.created_by_id == creator_id,
                UploadTransportBatch.request_key_sha256 == key_hash,
            )
            .with_for_update()
        )
        if batch is None:
            raise RuntimeError("Conflicting upload batch registration could not be read back")
        if batch.manifest_sha256 != digest:
            raise UploadTransportConflict("Registration key was already used for another manifest")
        return batch, False

    db.add_all(
        UploadTransportBatchItem(
            batch_id=inserted_id,
            file_index=index,
            chunk_index=index // files_per_chunk,
            filename=item["filename"],
            file_size=item["size"],
            content_sha256=item["sha256"],
        )
        for index, item in enumerate(files)
    )
    db.flush()
    batch = db.get(UploadTransportBatch, inserted_id)
    if batch is None:
        raise RuntimeError("New upload batch disappeared inside its transaction")
    return batch, True


def reprocess_batch(
    db: Session,
    *,
    parent_id: str,
    creator_id: int,
    request_key: str,
) -> tuple[UploadTransportBatch, bool]:
    """Create a new immutable local-processing plan linked to a completed batch."""
    if db.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Shared upload batch reprocessing requires PostgreSQL")
    key_hash = reprocess_key_sha256(request_key)
    parent = db.scalar(
        select(UploadTransportBatch)
        .where(UploadTransportBatch.id == parent_id)
        .with_for_update()
    )
    if parent is None:
        raise UploadTransportConflict("Reprocessing parent does not exist")

    existing = db.scalar(
        select(UploadTransportBatch).where(
            UploadTransportBatch.reprocess_request_key_sha256 == key_hash
        )
    )
    if existing is not None:
        if (existing.reprocess_parent_id != parent.id
                or existing.reprocess_parent_manifest_sha256 != parent.manifest_sha256):
            raise UploadTransportConflict("Reprocessing key was already used for another parent")
        return existing, False

    parent_receipts = receipt_map(db, parent.id)
    expected_chunks = (parent.file_count + parent.files_per_chunk - 1) // parent.files_per_chunk
    if set(parent_receipts) != set(range(expected_chunks)):
        raise UploadTransportIncomplete("Only a fully accepted batch can be explicitly reprocessed")

    child_id = secrets.token_hex(16)
    child_request_hash = request_key_sha256(secrets.token_hex(24))
    inserted_id = db.scalar(
        postgresql_insert(UploadTransportBatch)
        .values(
            id=child_id,
            created_by_id=creator_id,
            request_key_sha256=child_request_hash,
            manifest_sha256=parent.manifest_sha256,
            files_per_chunk=parent.files_per_chunk,
            file_count=parent.file_count,
            total_bytes=parent.total_bytes,
            reprocess_parent_id=parent.id,
            reprocess_parent_manifest_sha256=parent.manifest_sha256,
            reprocess_request_key_sha256=key_hash,
        )
        .on_conflict_do_nothing(constraint="uq_upload_transport_batch_reprocess_key")
        .returning(UploadTransportBatch.id)
    )
    if inserted_id is None:
        existing = db.scalar(
            select(UploadTransportBatch).where(
                UploadTransportBatch.reprocess_request_key_sha256 == key_hash
            )
        )
        if existing is None:
            raise RuntimeError("Reprocessing replay could not be read back")
        if (existing.reprocess_parent_id != parent.id
                or existing.reprocess_parent_manifest_sha256 != parent.manifest_sha256):
            raise UploadTransportConflict("Reprocessing key was already used for another parent")
        return existing, False

    source_items = list(db.scalars(
        select(UploadTransportBatchItem)
        .where(UploadTransportBatchItem.batch_id == parent.id)
        .order_by(UploadTransportBatchItem.file_index)
    ))
    db.add_all(
        UploadTransportBatchItem(
            batch_id=inserted_id,
            file_index=item.file_index,
            chunk_index=item.chunk_index,
            filename=item.filename,
            file_size=item.file_size,
            content_sha256=item.content_sha256,
        )
        for item in source_items
    )
    db.flush()
    child = db.get(UploadTransportBatch, inserted_id)
    if child is None:
        raise RuntimeError("New reprocessing batch disappeared inside its transaction")
    return child, True


def chunk_items(db: Session, batch_id: str, chunk_index: int) -> list[UploadTransportBatchItem]:
    return list(
        db.scalars(
            select(UploadTransportBatchItem)
            .where(
                UploadTransportBatchItem.batch_id == batch_id,
                UploadTransportBatchItem.chunk_index == chunk_index,
            )
            .order_by(UploadTransportBatchItem.file_index)
        )
    )


def receipt_map(db: Session, batch_id: str) -> dict[int, UploadTransportChunkReceipt]:
    rows = db.scalars(
        select(UploadTransportChunkReceipt).where(
            UploadTransportChunkReceipt.batch_id == batch_id
        )
    )
    return {row.chunk_index: row for row in rows}


def public_batch_state(
    db: Session, batch: UploadTransportBatch, *, include_files: bool = False
) -> dict:
    receipts = receipt_map(db, batch.id)
    chunk_count = (batch.file_count + batch.files_per_chunk - 1) // batch.files_per_chunk
    state = {
        "id": batch.id,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "file_count": batch.file_count,
        "files_per_chunk": batch.files_per_chunk,
        "chunk_count": chunk_count,
        "reprocess_parent_id": batch.reprocess_parent_id,
    }
    if include_files:
        state["request_key_sha256"] = batch.request_key_sha256
        files = db.scalars(
            select(UploadTransportBatchItem)
            .where(UploadTransportBatchItem.batch_id == batch.id)
            .order_by(UploadTransportBatchItem.file_index)
        )
        state["files"] = [
            {"filename": item.filename, "size": item.file_size, "sha256": item.content_sha256}
            for item in files
        ]
    state["chunks"] = [
            {
                "index": index,
                "accepted": index in receipts,
                "operation_id": receipts[index].operation_id if index in receipts else None,
                "accepted_by_id": receipts[index].accepted_by_id if index in receipts else None,
                "accepted_at": receipts[index].accepted_at.isoformat() if index in receipts else None,
            }
            for index in range(chunk_count)
    ]
    return state
