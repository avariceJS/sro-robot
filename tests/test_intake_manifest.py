"""Intake manifest contracts keep content and source metadata distinct."""

from __future__ import annotations

import unittest

from hypothesis import assume, given, settings, strategies as st

from app.services.tasks import (
    intake_source_metadata_manifest,
    begin_intake_operation,
)


class IntakeSourceMetadataManifestTests(unittest.TestCase):
    @settings(max_examples=250, derandomize=True, deadline=None, database=None)
    @given(left=st.text(), right=st.text())
    def test_length_framing_separates_arbitrary_filename_boundaries(
        self, left: str, right: str
    ) -> None:
        self.assertNotEqual(
            intake_source_metadata_manifest([left + right]),
            intake_source_metadata_manifest([left, right]),
        )

    @settings(max_examples=250, derandomize=True, deadline=None, database=None)
    @given(filenames=st.lists(st.text(), max_size=12))
    def test_appending_empty_filename_always_changes_manifest(self, filenames: list[str]) -> None:
        self.assertNotEqual(
            intake_source_metadata_manifest(filenames),
            intake_source_metadata_manifest([*filenames, ""]),
        )

    @settings(max_examples=250, derandomize=True, deadline=None, database=None)
    @given(first=st.text(), second=st.text().filter(lambda value: value != ""))
    def test_reordering_distinct_filenames_changes_manifest(
        self, first: str, second: str
    ) -> None:
        assume(first != second)
        self.assertNotEqual(
            intake_source_metadata_manifest([first, second]),
            intake_source_metadata_manifest([second, first]),
        )

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

    def test_digest_must_be_lowercase_sha256_before_database_access(self) -> None:
        invalid_digests = ("a" * 63, "A" * 64, "g" * 64)
        for digest in invalid_digests:
            with self.subTest(digest=digest[:4]), self.assertRaisesRegex(
                ValueError, "lowercase SHA-256"
            ):
                begin_intake_operation(
                    None,  # type: ignore[arg-type]
                    user_id=1,
                    idempotency_key="a" * 48,
                    manifest_sha256="b" * 64,
                    source_metadata_sha256=digest,
                )


if __name__ == "__main__":
    unittest.main()
