"""Bounded queue pool admission for isolated SRO browser jobs."""

import unittest

from scripts.dev.run_worker import validate_worker_configuration


class PreviewWorkerPoolTests(unittest.TestCase):
    def test_isolated_preview_pool_admits_two_to_four_workers(self):
        for workers in (2, 3, 4):
            validate_worker_configuration(workers, ["sro_preview"], False)

    def test_mixed_queue_or_unleased_ocr_pool_is_rejected(self):
        for queues, leases in (
            (["ocr"], False),
            (["sro_preview", "ocr"], True),
            (["sro"], True),
        ):
            with self.subTest(queues=queues, leases=leases), self.assertRaises(ValueError):
                validate_worker_configuration(2, queues, leases)

    def test_oversized_pool_is_rejected(self):
        for workers in (0, 5):
            with self.assertRaises(ValueError):
                validate_worker_configuration(workers, ["sro_preview"], True)


if __name__ == "__main__":
    unittest.main()
