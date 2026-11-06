"""Synthetic list read bounds; no real database, document, or staff data."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.api.routes_upload_batches import list_upload_batches
from app.services import upload_transport


class UploadTransportReadsTests(unittest.TestCase):
    def test_fifty_visible_batches_share_one_receipt_query(self):
        batches = [SimpleNamespace(
            id=f'{i:032x}', created_at=None, file_count=2, files_per_chunk=1,
            reprocess_parent_id=None,
        ) for i in range(51)]
        receipts = [SimpleNamespace(
            batch_id=b.id, chunk_index=0, operation_id=100 + i,
            accepted_by_id=7, accepted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ) for i, b in enumerate(batches[:50])]
        db = MagicMock()
        db.scalars.side_effect = [batches, receipts]
        with patch.object(upload_transport, 'receipt_map', side_effect=AssertionError('N+1 query')):
            response = list_upload_batches(user=SimpleNamespace(id=7), db=db, page=0)
        payload = json.loads(response.body)
        self.assertTrue(payload['has_more'])
        self.assertEqual(len(payload['batches']), 50)
        self.assertEqual(db.scalars.call_count, 2)
        self.assertEqual(payload['batches'][49]['chunks'][0]['operation_id'], 149)
        self.assertFalse(payload['batches'][49]['chunks'][1]['accepted'])

    def test_empty_list_does_not_query_receipts(self):
        db = MagicMock()
        db.scalars.return_value = []
        response = list_upload_batches(user=SimpleNamespace(id=7), db=db, page=0)
        self.assertEqual(json.loads(response.body)['batches'], [])
        self.assertEqual(db.scalars.call_count, 1)

    def test_single_batch_keeps_receipt_lookup_and_empty_prefetch_stays_empty(self):
        batch = SimpleNamespace(id='a' * 32, created_at=None, file_count=1,
                                files_per_chunk=1, reprocess_parent_id=None)
        with patch.object(upload_transport, 'receipt_map', return_value={}) as receipts:
            upload_transport.public_batch_state(MagicMock(), batch)
            self.assertEqual(receipts.call_count, 1)
            upload_transport.public_batch_state(MagicMock(), batch, receipts={})
            self.assertEqual(receipts.call_count, 1)
