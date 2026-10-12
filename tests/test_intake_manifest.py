"""Intake manifest contracts keep content and source metadata distinct."""

from __future__ import annotations

import unittest

from app.services.tasks import intake_source_metadata_manifest


class IntakeSourceMetadataManifestTests(unittest.TestCase):
    def test_digest_is_deterministic_for_exact_ordered_filenames(self) -> None:
        filenames = ["Постановление.pdf", "Приложение.pdf"]

        self.assertEqual(
            intake_source_metadata_manifest(filenames),
            intake_source_metadata_manifest(list(filenames)),
        )

    def test_filename_value_and_order_are_bound(self) -> None:
        baseline = intake_source_metadata_manifest(["first.pdf", "second.pdf"])

        self.assertNotEqual(
            baseline,
            intake_source_metadata_manifest(["renamed.pdf", "second.pdf"]),
        )
        self.assertNotEqual(
            baseline,
            intake_source_metadata_manifest(["second.pdf", "first.pdf"]),
        )

    def test_length_prefixes_distinguish_ambiguous_concatenations(self) -> None:
        self.assertNotEqual(
            intake_source_metadata_manifest(["ab", "c"]),
            intake_source_metadata_manifest(["a", "bc"]),
        )

    def test_empty_manifest_and_empty_filename_are_distinct(self) -> None:
        self.assertNotEqual(
            intake_source_metadata_manifest([]),
            intake_source_metadata_manifest([""]),
        )

    def test_non_string_filename_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be strings"):
            intake_source_metadata_manifest(["valid.pdf", None])  # type: ignore[list-item]


if __name__ == "__main__":
    unittest.main()
