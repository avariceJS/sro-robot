"""Synthetic integrity tests for the private court evaluation dataset."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from PIL import Image

from scripts.dev.quality import court_dataset as dataset


class CourtDatasetTests(unittest.TestCase):
    def test_import_does_not_consume_pending_browser_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            root = parent / "dataset"; root.mkdir(mode=0o700)
            inputs = parent / "inbox"; inputs.mkdir()
            stage = inputs / ".native-kad-save-pending"; stage.mkdir()
            (stage / "document.pdf").write_bytes(b"incomplete synthetic save")
            Image.new("RGB", (20, 30), "white").save(inputs / "synthetic.png")
            result = dataset.ingest(root, inputs, "manual-public-import", "unknown")
            self.assertEqual(result["added"], 1)

    def test_cli_dataset_lock_refuses_concurrent_owner_and_releases_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaisesRegex(ValueError, "synthetic interruption"):
                with dataset.dataset_lock(root):
                    with self.assertRaisesRegex(RuntimeError, "owns the lock"):
                        with dataset.dataset_lock(root):
                            self.fail("Concurrent dataset operation admitted")
                    raise ValueError("synthetic interruption")
            with dataset.dataset_lock(root):
                pass

    def root(self, parent):
        root = (Path(parent) / "private-dataset").resolve()
        root.mkdir(mode=0o700)
        return root

    def import_images(self, root, parent, count=1, *, previously_seen=False):
        source = Path(parent) / "synthetic-inputs"
        source.mkdir(exist_ok=True)
        for index in range(count):
            image = Image.new("RGB", (40, 60), "white")
            # Deterministic geometric marks provide distinct synthetic bytes/pages.
            for x in range(index + 1):
                image.putpixel((x, x), (0, 0, 0))
            image.save(source / f"synthetic-{index}.png")
        dataset.ingest(root, source, "manual-public-import", "synthetic_distortion", previously_seen)
        return dataset.load_manifest(root)

    def add_score_evidence(self, root, doc, *, present_value="Synthetic Person", absent_observation=None,
                           transcript="synthetic words", primary_text="synthetic words", origin="ocr"):
        ref = dataset.reference_template(doc)
        ref.update(reviewer="Reviewer A", second_reviewer="Reviewer B", original_inspected=True, transcript=transcript)
        ref["fields"]["fio_raw"].update(state="present", value=present_value, page=1,
            participant="debtor-1", role="debtor", occurrence="p1-o1")
        ref["fields"]["inn"].update(state="absent")
        dataset.write_json(root / "references" / f"{doc['id']}.json", ref)
        captured = {
            "input_sha256": doc["sha256"],
            "capture": {"revision": "synthetic-revision", "source_dirty": False, "dirty_sha256": "",
                        "runtime": {"platform": "synthetic", "ocr_executables": []},
                        "seconds": 0.25, "completed_ocr": 1 if origin == "ocr" else 0},
            "observed_pipeline": {
                "fio_raw": {"value": present_value},
                "inn": {"value": absent_observation},
            },
            "variants": ["synthetic only"],
            "primary_pages": [{"page": 1, "text": primary_text, "origin": origin}],
        }
        dataset.write_json(root / "captures" / f"{doc['id']}.json", captured)
        audit = {"document_id": doc["id"], "capture_sha256": dataset.identity(captured),
                 "reference_sha256": dataset.identity(ref), "reviewer": "Reviewer C", "original_inspected": True,
                 "fields": {name: {"owner": "unreviewed", "source": "unreviewed", "error_layer": None}
                            for name in dataset.FIELDS}}
        audit["fields"]["fio_raw"] = {"owner": "wrong", "source": "correct", "error_layer": "participant_attribution"}
        audit["fields"]["inn"] = {"owner": "unreviewed", "source": "unreviewed", "error_layer": "normalization"}
        dataset.write_json(root / "audits" / f"{doc['id']}.json", audit)
        dataset.write_json(root / "baseline.json", dataset.cohort(captured))
        return ref, captured

    def test_case_page_near_duplicate_and_cluster_links_never_cross_splits(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = self.root(base)
            manifest = self.import_images(root, base, count=6)
            docs = manifest["documents"]
            # Four independent collision routes, including a transitive chain.
            docs[0]["case_key"] = docs[1]["case_key"] = "case-shared"
            docs[2]["pages"] = copy.deepcopy(docs[3]["pages"])
            docs[4]["pages"][0]["dhash"] = docs[5]["pages"][0]["dhash"]
            docs[4]["pages"][0]["aspect"] = docs[5]["pages"][0]["aspect"]
            docs[4]["duplicate_cluster"] = "cluster-shared"
            docs[5]["duplicate_cluster"] = "cluster-shared"
            for doc in docs:
                doc["case_reviewed"] = doc["duplicate_reviewed"] = True
                doc["case_key"] = doc["case_key"] or f"case-{doc['id'][:8]}"
            dataset.write_json(root / "manifest.json", manifest)
            split = dataset.freeze(root)
            groups = dataset.groups(docs)
            for group in groups:
                self.assertEqual(len({split["assignments"][d["id"]] for d in group}), 1)
            self.assertTrue(any({d["id"] for d in group} >= {docs[0]["id"], docs[1]["id"]} for group in groups))
            self.assertTrue(any({d["id"] for d in group} >= {docs[2]["id"], docs[3]["id"]} for group in groups))
            self.assertTrue(any({d["id"] for d in group} >= {docs[4]["id"], docs[5]["id"]} for group in groups))

    def test_previous_exposure_forces_whole_linked_group_to_development(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = self.root(base)
            manifest = self.import_images(root, base, count=2)
            docs = manifest["documents"]
            docs[0]["case_key"] = docs[1]["case_key"] = "one-case"
            docs[0]["previously_seen"] = True
            for doc in docs:
                doc["case_reviewed"] = doc["duplicate_reviewed"] = True
            dataset.write_json(root / "manifest.json", manifest)
            assignments = dataset.freeze(root)["assignments"]
            self.assertEqual({assignments[d["id"]] for d in docs}, {"development"})

    def test_unknown_case_is_quarantined(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = self.root(base)
            manifest = self.import_images(root, base)
            self.assertEqual(dataset.freeze(root)["assignments"][manifest["documents"][0]["id"]], "quarantine")

    def test_frozen_manifest_cannot_be_extended_or_mutated(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = self.root(base)
            manifest = self.import_images(root, base)
            manifest["documents"][0]["case_key"] = "reviewed-case"
            manifest["documents"][0]["case_reviewed"] = True
            manifest["documents"][0]["duplicate_reviewed"] = True
            dataset.write_json(root / "manifest.json", manifest)
            dataset.freeze(root)
            manifest["documents"][0]["case_key"] = "changed-after-freeze"
            dataset.write_json(root / "manifest.json", manifest)
            with self.assertRaisesRegex(ValueError, "manifest changed"):
                dataset.freeze(root)

    def test_existing_holdout_with_prior_exposure_or_unreviewed_case_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = self.root(base)
            manifest = self.import_images(root, base)
            doc = manifest["documents"][0]
            doc.update(case_key="known-case", case_reviewed=True, duplicate_reviewed=True, previously_seen=True)
            dataset.write_json(root / "manifest.json", manifest)
            split = dataset.freeze(root)
            split["assignments"][doc["id"]] = "holdout"
            dataset.write_json(root / "split.json", split)
            with self.assertRaisesRegex(ValueError, "Unqualified holdout"):
                dataset.validate_split(root, manifest)

    def test_asset_bytes_are_verified_against_content_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = self.root(base)
            doc = self.import_images(root, base)["documents"][0]
            target = root / "objects" / (doc["sha256"] + doc["extension"])
            target.write_bytes(b"changed synthetic bytes")
            with self.assertRaisesRegex(ValueError, "integrity"):
                dataset.asset_path(root, doc)

    def test_reference_requires_two_humans_original_read_and_valid_present_page(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = self.root(base)
            doc = self.import_images(root, base)["documents"][0]
            ref = dataset.reference_template(doc)
            ref.update(reviewer="Reviewer A", second_reviewer="Reviewer B", original_inspected=True)
            field = ref["fields"]["fio_raw"]
            field.update(state="present", value="Synthetic Person", page=1, participant="debtor-1", role="debtor", occurrence="p1-o1")
            dataset.validate_reference(ref, doc)
            for change in (
                {"second_reviewer": "Reviewer A"}, {"second_reviewer": " reviewer a "},
                {"original_inspected": False},
            ):
                broken = copy.deepcopy(ref); broken.update(change)
                with self.assertRaises(ValueError):
                    dataset.validate_reference(broken, doc)
            for key, value in (("page", True), ("page", 2), ("participant", ""), ("occurrence", "")):
                broken = copy.deepcopy(ref); broken["fields"]["fio_raw"][key] = value
                with self.assertRaises(ValueError):
                    dataset.validate_reference(broken, doc)

    def test_unknown_absent_and_disputed_references_cannot_claim_truth(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = self.root(base)
            doc = self.import_images(root, base)["documents"][0]
            ref = dataset.reference_template(doc)
            ref.update(reviewer="A", second_reviewer="B", original_inspected=True)
            dataset.validate_reference(ref, doc)
            for state in ("absent", "ambiguous", "unreadable", "unreviewed"):
                ref["fields"]["inn"].update(state=state, value="invented")
                with self.assertRaises(ValueError):
                    dataset.validate_reference(ref, doc)
                ref["fields"]["inn"]["value"] = None
            ref["disputed"] = True
            with self.assertRaisesRegex(ValueError, "disputed"):
                dataset.validate_reference(ref, doc)

    def test_reference_schema_and_transcript_page_slots_are_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = self.root(base)
            doc = self.import_images(root, base)["documents"][0]
            ref = dataset.reference_template(doc)
            ref.update(reviewer="A", second_reviewer="B", original_inspected=True, transcript="one page\fsecond page")
            with self.assertRaisesRegex(ValueError, "page slots"):
                dataset.validate_reference(ref, doc)
            ref["transcript"] = "one page"
            del ref["fields"]["snils"]
            with self.assertRaisesRegex(ValueError, "schema"):
                dataset.validate_reference(ref, doc)

    def test_symlinked_private_root_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as external:
            link = Path(temporary) / "dataset-link"
            link.symlink_to(Path(external), target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "Symlinked"):
                dataset.verify_storage(link)

    def test_import_reuses_hash_identical_object_after_manifest_write_interruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = self.root(base)
            source = base / "inputs"; source.mkdir()
            Image.new("RGB", (40, 60), "white").save(source / "synthetic.png")
            original_write_json = dataset.write_json
            def interrupt_manifest(path, value):
                if Path(path) == root / "manifest.json":
                    raise OSError("synthetic interruption")
                return original_write_json(path, value)
            with patch.object(dataset, "write_json", side_effect=interrupt_manifest):
                with self.assertRaises(OSError):
                    dataset.ingest(root, source, "manual-public-import", "scan")
            object_dir = root / "objects"
            objects = list(object_dir.iterdir())
            self.assertEqual(len(objects), 1)
            first_hash = hashlib.sha256(objects[0].read_bytes()).hexdigest()
            result = dataset.ingest(root, source, "manual-public-import", "scan")
            self.assertEqual(result["added"], 1)
            self.assertEqual(hashlib.sha256(objects[0].read_bytes()).hexdigest(), first_hash)

    def test_original_publication_failure_leaves_no_partial_final_and_retry_is_hash_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "objects"; directory.mkdir(mode=0o700)
            target = directory / "synthetic-original.png"
            data = b"complete synthetic image bytes"
            with patch.object(dataset.os, "link", side_effect=OSError("synthetic link failure")):
                with self.assertRaises(OSError):
                    dataset.publish_original(target, data)
            self.assertFalse(target.exists())
            self.assertEqual(list(directory.iterdir()), [])
            dataset.publish_original(target, data)
            self.assertEqual(target.read_bytes(), data)
            dataset.publish_original(target, data)
            with self.assertRaisesRegex(ValueError, "integrity mismatch"):
                dataset.publish_original(target, b"different synthetic bytes")
            self.assertEqual(target.read_bytes(), data)

    def test_bad_acquisition_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = self.root(base); source = base / "inputs"; source.mkdir()
            with self.assertRaisesRegex(ValueError, "metadata"):
                dataset.ingest(root, source, "private-cloud", "scan")
            with self.assertRaisesRegex(ValueError, "metadata"):
                dataset.ingest(root, source, "manual-public-import", "medical-record")

    def test_private_path_cannot_point_into_checkout(self):
        path = dataset.ROOT / "tests" / "synthetic-private-data-must-not-be-created"
        with self.assertRaisesRegex(ValueError, "outside the checkout"):
            dataset.private_root(path)

    def test_score_keeps_false_positives_and_human_error_layers_and_separates_weak_text_cer(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = self.root(base)
            manifest = self.import_images(root, base)
            doc = manifest["documents"][0]
            doc.update(previously_seen=True, case_key="seen-case")
            dataset.write_json(root / "manifest.json", manifest)
            dataset.freeze(root)
            self.add_score_evidence(root, doc, absent_observation="synthetic false positive",
                                    transcript="synthetic words", primary_text="synthetic wordz",
                                    origin="embedded-text-weak")
            report = dataset.score(root)["partitions"]["development"]
            counts = report["counts"]
            self.assertEqual(counts["false_positive"], 1)
            self.assertEqual(counts.get("correct_absence", 0), 0)
            self.assertEqual(report["human_error_categories"]["participant_attribution"], 1)
            self.assertEqual(report["human_error_categories"]["normalization"], 1)
            self.assertEqual(counts["exact_correct"], 1)
            self.assertNotIn("qualified_correct", counts)
            self.assertEqual(report["qualified_value_owner_source_accuracy"], 0)
            self.assertIsNone(report["reading_order_cer"])
            self.assertEqual(report["embedded_text_comparison_cer"], 1 / len("synthetic words"))

    def test_ambiguous_owner_keeps_qualified_accuracy_unqualified(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = self.root(base)
            manifest = self.import_images(root, base)
            doc = manifest["documents"][0]
            doc.update(previously_seen=True, case_key="seen-case")
            dataset.write_json(root / "manifest.json", manifest)
            dataset.freeze(root)
            self.add_score_evidence(root, doc)
            audit_path = root / "audits" / f"{doc['id']}.json"
            audit = dataset.read_json(audit_path)
            audit["fields"]["fio_raw"]["owner"] = "ambiguous"
            dataset.write_json(audit_path, audit)
            report = dataset.score(root)["partitions"]["development"]
            self.assertEqual(report["counts"]["exact_correct"], 1)
            self.assertNotIn("qualified_correct", report["counts"])
            self.assertIsNone(report["qualified_value_owner_source_accuracy"])

    def test_missing_capture_is_not_scored_as_correct_absence_and_empty_denominators_are_null(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = self.root(base)
            manifest = self.import_images(root, base)
            doc = manifest["documents"][0]
            doc.update(previously_seen=True, case_key="seen-case")
            dataset.write_json(root / "manifest.json", manifest)
            dataset.freeze(root)
            ref = dataset.reference_template(doc)
            ref.update(reviewer="Reviewer A", second_reviewer="Reviewer B", original_inspected=True)
            ref["fields"]["inn"]["state"] = "absent"
            dataset.write_json(root / "references" / f"{doc['id']}.json", ref)
            report = dataset.score(root)["partitions"]["development"]
            self.assertEqual(report["counts"]["missing_capture"], 1)
            self.assertEqual(report["counts"].get("correct_absence", 0), 0)
            self.assertEqual(report["counts"]["unobserved_reference_fields"], 1)
            self.assertIsNone(report["exact_value_accuracy"])
            self.assertIsNone(report["qualified_value_owner_source_accuracy"])

    def test_capture_from_a_different_source_runtime_cohort_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = self.root(base)
            manifest = self.import_images(root, base)
            doc = manifest["documents"][0]
            doc.update(previously_seen=True, case_key="seen-case")
            dataset.write_json(root / "manifest.json", manifest)
            dataset.freeze(root)
            _, captured = self.add_score_evidence(root, doc)
            baseline = dataset.cohort(captured)
            baseline["source"]["revision"] = "different-synthetic-revision"
            dataset.write_json(root / "baseline.json", baseline)
            with self.assertRaisesRegex(ValueError, "source/runtime differs"):
                dataset.score(root)

    def test_unknown_transcript_origin_is_reported_without_entering_ocr_cer(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = self.root(base)
            manifest = self.import_images(root, base)
            doc = manifest["documents"][0]
            doc.update(previously_seen=True, case_key="seen-case")
            dataset.write_json(root / "manifest.json", manifest)
            dataset.freeze(root)
            self.add_score_evidence(root, doc, origin="unknown", transcript="synthetic words",
                                    primary_text="unqualified source text")
            report = dataset.score(root)["partitions"]["development"]
            self.assertEqual(report["counts"]["unknown_origin_transcript_pages"], 1)
            self.assertIsNone(report["reading_order_cer"])
            self.assertIsNone(report["embedded_text_comparison_cer"])

    def test_unreviewed_reference_fields_do_not_create_an_accuracy_denominator(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = self.root(base)
            manifest = self.import_images(root, base)
            doc = manifest["documents"][0]
            doc.update(previously_seen=True, case_key="seen-case")
            dataset.write_json(root / "manifest.json", manifest)
            dataset.freeze(root)
            ref = dataset.reference_template(doc)
            ref.update(reviewer="Reviewer A", second_reviewer="Reviewer B", original_inspected=True)
            dataset.write_json(root / "references" / f"{doc['id']}.json", ref)
            report = dataset.score(root)["partitions"]["development"]
            self.assertEqual(report["counts"]["human_reference_documents"], 1)
            self.assertEqual(report["counts"].get("positive_fields", 0), 0)
            self.assertIsNone(report["exact_value_accuracy"])
            self.assertIsNone(report["qualified_value_owner_source_accuracy"])

    def test_score_cli_emits_only_public_aggregate_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.root(Path(temporary))
            dataset.write_json(root / "manifest.json", {"schema": 1, "documents": []})
            dataset.freeze(root)
            stdout = io.StringIO()
            with patch.object(dataset, "private_root", return_value=root), redirect_stdout(stdout):
                self.assertEqual(dataset.main(["--private-root", str(root), "score"]), 0)
            emitted = json.loads(stdout.getvalue())
            self.assertEqual(set(emitted), {"accuracy_status", "readiness", "partitions"})
            self.assertNotIn("baseline", emitted)
            self.assertNotIn("manifest_sha256", emitted)


if __name__ == "__main__":
    unittest.main()
