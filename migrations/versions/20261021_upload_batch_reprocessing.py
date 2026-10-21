"""add explicit immutable lineage for repeated shared-batch processing

Revision ID: 20261021_reproc
Revises: d74b20c6a913
Create Date: 2026-10-21
"""

from __future__ import annotations

import hashlib

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision = "20261021_reproc"
down_revision = "d74b20c6a913"
branch_labels = None
depends_on = None


_REQUIRE_COMPLETE_FUNCTION = """
CREATE OR REPLACE FUNCTION public.sro_require_upload_transport_manifest_complete()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $upload_transport_manifest_complete$
DECLARE
    batch_id_value text;
    parent_id_value text;
    declared_files integer;
    declared_bytes bigint;
    chunk_size integer;
    actual_files bigint;
    actual_bytes bigint;
    parent_row public.upload_transport_batch%ROWTYPE;
    child_row public.upload_transport_batch%ROWTYPE;
BEGIN
    batch_id_value := coalesce(to_jsonb(NEW)->>'id', to_jsonb(NEW)->>'batch_id');
    SELECT * INTO child_row FROM public.upload_transport_batch WHERE id = batch_id_value;
    IF NOT FOUND THEN RETURN NULL; END IF;
    declared_files := child_row.file_count;
    declared_bytes := child_row.total_bytes;
    chunk_size := child_row.files_per_chunk;
    SELECT count(*), coalesce(sum(file_size), 0) INTO actual_files, actual_bytes
      FROM public.upload_transport_batch_item WHERE batch_id = batch_id_value;
    IF actual_files <> declared_files OR actual_bytes <> declared_bytes
       OR EXISTS (
          SELECT 1 FROM public.upload_transport_batch_item item
           WHERE item.batch_id = batch_id_value
             AND (item.file_index >= declared_files
               OR item.chunk_index <> item.file_index / chunk_size)
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'upload transport manifest is incomplete or inconsistent';
    END IF;
    parent_id_value := child_row.reprocess_parent_id;
    IF parent_id_value IS NULL THEN RETURN NULL; END IF;
    SELECT * INTO parent_row FROM public.upload_transport_batch
     WHERE id = parent_id_value
       AND manifest_sha256 = child_row.reprocess_parent_manifest_sha256;
    IF NOT FOUND
       OR child_row.manifest_sha256 <> parent_row.manifest_sha256
       OR child_row.file_count <> parent_row.file_count
       OR child_row.files_per_chunk <> parent_row.files_per_chunk
       OR child_row.total_bytes <> parent_row.total_bytes THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'reprocessing batch parent context does not match';
    END IF;
    IF (SELECT count(*) FROM public.upload_transport_chunk_receipt WHERE batch_id = parent_id_value)
       <> (parent_row.file_count + parent_row.files_per_chunk - 1) / parent_row.files_per_chunk THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'reprocessing parent batch is not fully accepted';
    END IF;
    IF EXISTS (
        (SELECT file_index, chunk_index, filename, file_size, content_sha256
           FROM public.upload_transport_batch_item WHERE batch_id = parent_id_value
         EXCEPT
         SELECT file_index, chunk_index, filename, file_size, content_sha256
           FROM public.upload_transport_batch_item WHERE batch_id = batch_id_value)
        UNION ALL
        (SELECT file_index, chunk_index, filename, file_size, content_sha256
           FROM public.upload_transport_batch_item WHERE batch_id = batch_id_value
         EXCEPT
         SELECT file_index, chunk_index, filename, file_size, content_sha256
           FROM public.upload_transport_batch_item WHERE batch_id = parent_id_value)
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'reprocessing batch manifest differs from its parent';
    END IF;
    RETURN NULL;
END;
$upload_transport_manifest_complete$;
"""


def upgrade() -> None:
    function_rows = op.get_bind().execute(text(
        "SELECT p.prosrc, language.lanname, p.proconfig, p.prosecdef "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "JOIN pg_language language ON language.oid=p.prolang "
        "WHERE n.nspname='public' AND p.proname='sro_require_upload_transport_manifest_complete' "
        "AND p.pronargs=0 AND p.prorettype='trigger'::regtype"
    )).all()
    if len(function_rows) != 1:
        raise RuntimeError("Cannot replace missing or ambiguous upload manifest guard")
    old_source, language, config, security_definer = function_rows[0]
    old_hash = hashlib.sha256(" ".join(old_source.split()).encode()).hexdigest()
    if (old_hash != "293fa2d5392dea950896c3cf2edbcee5f60300dac42bf6230eb3b84230059498"
            or language != "plpgsql" or not config or "search_path=pg_catalog" not in config
            or security_definer):
        raise RuntimeError("Refusing to replace a drifted upload manifest guard")
    op.add_column("upload_transport_batch", sa.Column("reprocess_parent_id", sa.String(32), nullable=True))
    op.add_column("upload_transport_batch", sa.Column("reprocess_parent_manifest_sha256", sa.String(64), nullable=True))
    op.add_column("upload_transport_batch", sa.Column("reprocess_request_key_sha256", sa.String(64), nullable=True))
    op.create_unique_constraint(
        "uq_upload_transport_batch_reprocess_key", "upload_transport_batch", ["reprocess_request_key_sha256"]
    )
    op.create_foreign_key(
        "fk_upload_transport_batch_reprocess_parent_context", "upload_transport_batch",
        "upload_transport_batch", ["reprocess_parent_id", "reprocess_parent_manifest_sha256"],
        ["id", "manifest_sha256"],
    )
    op.create_check_constraint(
        "ck_upload_transport_batch_reprocess_context", "upload_transport_batch",
        "(reprocess_parent_id IS NULL AND reprocess_parent_manifest_sha256 IS NULL AND reprocess_request_key_sha256 IS NULL) OR "
        "(reprocess_parent_id IS NOT NULL AND reprocess_parent_manifest_sha256 IS NOT NULL AND reprocess_request_key_sha256 IS NOT NULL AND reprocess_parent_manifest_sha256 ~ '^[0-9a-f]{64}$' AND reprocess_request_key_sha256 ~ '^[0-9a-f]{64}$' AND reprocess_parent_id <> id)",
    )
    op.create_check_constraint(
        "ck_upload_transport_batch_reprocess_key_sha256", "upload_transport_batch",
        "reprocess_request_key_sha256 IS NULL OR reprocess_request_key_sha256 ~ '^[0-9a-f]{64}$'",
    )
    op.execute(_REQUIRE_COMPLETE_FUNCTION)


def downgrade() -> None:
    raise RuntimeError("Shared upload reprocessing lineage is durable intake evidence and must not be removed automatically.")
