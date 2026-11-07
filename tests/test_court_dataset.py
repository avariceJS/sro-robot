"""Synthetic integrity tests for the private court evaluation dataset."""
import copy
import hashlib
import io
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from PIL import Image

from scripts.dev.quality import court_dataset as dataset


class CourtDatasetTests(unittest.TestCase):
    def review_ledger(self, parent, document_id, metadata=None):
        path = parent.resolve() / "owner-exposure.json"
        dataset.write_json(path, {"schema": 1, "exposures": [{"document_id": document_id,
            "asset_sha256": document_id, "first_seen_at": "2099-01-01T00:00:00Z", "metadata": metadata or {}}]})
        return path

    def test_review_ledger_exposes_historical_case_alias_component_only_in_new_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary); first = self.root(parent)
            manifest = self.independent_manifest(first, parent, count=2)
            for index in range(100):
                case_id = f"11111111-1111-4111-8111-{index:012x}"
                key = "official-case:" + case_id
                if int(dataset.digest(("sro-court-eval-v1:" + key).encode())[:8], 16) % 5 == 0:
                    break
            for doc in manifest["documents"]:
                doc["case_key"] = key
            dataset.write_json(first / "manifest.json", manifest)
            self.assertEqual(set(dataset.freeze(first)["assignments"].values()), {"holdout"})
            original = (first / "manifest.json").read_bytes()
            second = parent.resolve() / "second"; second.mkdir(mode=0o700)
            dataset.extend(second, first)
            updated = dataset.load_manifest(second)
            updated["documents"][0]["case_key"] = "synthetic-canonical-correction"
            dataset.write_json(second / "manifest.json", updated); dataset.freeze(second)
            ledger = self.review_ledger(parent, "f" * 64, {"case_id": case_id.upper()})
            third = parent.resolve() / "third"; third.mkdir(mode=0o700)
            dataset.extend(third, second, review_exposure_ledger=ledger)
            self.assertEqual(set(dataset.freeze(third)["assignments"].values()), {"development"})
            self.assertEqual((first / "manifest.json").read_bytes(), original)
            self.assertEqual(set(dataset.validate_split(second, updated)["assignments"].values()), {"holdout"})
            self.assertFalse((third / "references").exists())

    def test_review_ledger_applies_to_new_assets_before_freeze_without_gold(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary); first = self.root(parent)
            manifest = self.independent_manifest(first, parent, count=1)
            old = manifest["documents"][0]; old["case_key"] = self.case_for("holdout")
            dataset.write_json(first / "manifest.json", manifest); dataset.freeze(first)
            second = parent.resolve() / "second"; second.mkdir(mode=0o700)
            inputs = parent / "new-assets"; inputs.mkdir()
            Image.new("RGB", (30, 37), (30, 70, 110)).save(inputs / "synthetic-review.png")
            new_id = dataset.digest((inputs / "synthetic-review.png").read_bytes())
            ledger = self.review_ledger(parent, new_id)
            dataset.extend(second, first, review_exposure_ledger=ledger)
            self.assertEqual(dataset.load_manifest(second)["review_exposure"]["matched_documents"], 0)
            dataset.ingest(second, inputs, "manual-public-import", "scan")
            updated = dataset.load_manifest(second); new = updated["documents"][1]
            new.update(case_key="synthetic-new-case", case_reviewed=True, duplicate_reviewed=True)
            new["pages"][0]["dhash"] = "0123456789abcdef"
            dataset.write_json(second / "manifest.json", updated)
            # A plain freeze still consumes the ledger selected at extend,
            # including previously unknown assets imported in between.
            split = dataset.freeze(second)
            self.assertEqual(split["assignments"][old["id"]], "holdout")
            self.assertEqual(split["assignments"][new["id"]], "development")
            frozen_bytes = (second / "manifest.json").read_bytes()
            self.assertEqual(dataset.freeze(second, review_exposure_ledger=ledger), split)
            self.assertEqual((second / "manifest.json").read_bytes(), frozen_bytes)
            self.assertFalse((second / "references").exists())
            with self.assertRaisesRegex(ValueError, "Frozen corpus"):
                dataset.apply_review_exposure(second, ledger)

    def test_review_ledger_rejects_public_permissions_symlink_and_invalid_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary); root = self.root(parent)
            manifest = self.independent_manifest(root, parent, count=1)
            ledger = self.review_ledger(parent, manifest["documents"][0]["id"])
            ledger.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "private permissions"):
                dataset.review_exposure_ids(manifest, ledger)
            ledger.chmod(0o600)
            link = parent / "linked-ledger.json"; link.symlink_to(ledger)
            with self.assertRaisesRegex(ValueError, "storage boundary"):
                dataset.review_exposure_ids(manifest, link)
            dataset.write_json(ledger, {"schema": 2, "exposures": []})
            with self.assertRaisesRegex(ValueError, "schema"):
                dataset.review_exposure_ids(manifest, ledger)

    def test_live_review_database_is_reread_after_imports_and_later_owner_exposure(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary); first = self.root(parent)
            manifest = self.independent_manifest(first, parent, count=1)
            old = manifest["documents"][0]; old["case_key"] = self.case_for("holdout")
            dataset.write_json(first / "manifest.json", manifest); dataset.freeze(first)
            original = (first / "manifest.json").read_bytes()
            database = parent.resolve() / "review.sqlite3"
            connection = sqlite3.connect(database)
            database.chmod(0o600)
            connection.executescript('''PRAGMA journal_mode=WAL;
                CREATE TABLE documents(id TEXT PRIMARY KEY,asset_sha256 TEXT,metadata_json TEXT);
                CREATE TABLE exposure(document_id TEXT PRIMARY KEY,first_seen_at TEXT);''')
            connection.commit()
            try:
                second = parent.resolve() / "second"; second.mkdir(mode=0o700)
                dataset.extend(second, first, review_exposure_ledger=database)
                inputs = parent / "new-assets"; inputs.mkdir()
                Image.new("RGB", (31, 37), (60, 80, 110)).save(inputs / "synthetic-live.png")
                dataset.ingest(second, inputs, "manual-public-import", "scan")
                current = dataset.load_manifest(second); new = current["documents"][1]
                new.update(case_key="synthetic-other-case", case_reviewed=True, duplicate_reviewed=True)
                new["pages"][0]["dhash"] = "0123456789abcdef"
                dataset.write_json(second / "manifest.json", current)
                # Feedback after extend is visible in the still-live WAL. Only
                # source identity metadata exists; no private text is selected.
                for doc in (old, new):
                    connection.execute("INSERT INTO documents VALUES(?,?,?)", (doc["id"], doc["sha256"], "{}"))
                    connection.execute("INSERT INTO exposure VALUES(?,?)", (doc["id"], "2099-01-01"))
                connection.commit()
                self.assertEqual(set(dataset.freeze(second)["assignments"].values()), {"development"})
                self.assertEqual((first / "manifest.json").read_bytes(), original)
                self.assertFalse((second / "references").exists())
            finally:
                connection.close()

    def test_later_review_of_frozen_control_fails_without_mutating_frozen_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary); root = self.root(parent)
            manifest = self.independent_manifest(root, parent, count=1)
            doc = manifest["documents"][0]; doc["case_key"] = self.case_for("holdout")
            dataset.write_json(root / "manifest.json", manifest)
            ledger = parent.resolve() / "owner-exposure.json"
            dataset.write_json(ledger, {"schema": 1, "exposures": []})
            dataset.freeze(root, review_exposure_ledger=ledger)
            frozen = (root / "manifest.json").read_bytes()
            self.review_ledger(parent, doc["id"])
            with self.assertRaisesRegex(ValueError, "Frozen corpus"):
                dataset.freeze(root)
            with self.assertRaisesRegex(ValueError, "Frozen corpus"):
                dataset.validate_split(root, dataset.load_manifest(root))
            with self.assertRaisesRegex(ValueError, "Frozen corpus"):
                dataset.score(root)
            successor = parent.resolve() / "after-review"; successor.mkdir(mode=0o700)
            dataset.extend(successor, root)
            self.assertEqual(set(dataset.freeze(successor)["assignments"].values()), {"development"})
            self.assertEqual((root / "manifest.json").read_bytes(), frozen)

    def independent_manifest(self, root, parent, count=3):
        manifest = self.import_images(root, parent, count=count)
        for index, doc in enumerate(manifest["documents"]):
            doc["pages"][0]["dhash"] = hashlib.sha256(f"synthetic fingerprint {index}".encode()).hexdigest()[:16]
            doc.update(case_key=f"synthetic-case-{index}", case_reviewed=True, duplicate_reviewed=True)
        dataset.write_json(root / "manifest.json", manifest)
        return manifest

    def case_for(self, partition):
        for index in range(100):
            key = f"synthetic-reviewed-case-{index}"
            assigned = int(dataset.digest(("sro-court-eval-v1:" + key).encode())[:8], 16) % 5 == 0
            if assigned == (partition == "holdout"):
                return key
        self.fail("No synthetic deterministic partition key")

    def test_import_is_resumable_beyond_200_without_an_unbounded_single_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary); root = self.root(parent)
            inputs = parent / "inputs"; inputs.mkdir()
            for index in range(205):
                Image.new("RGB", (5, 7), (index, 30, 40)).save(inputs / f"{index:03}.png")
            results = [dataset.ingest(root, inputs, "manual-public-import", "scan", limit=75) for _ in range(3)]
            self.assertEqual([r["added"] for r in results], [75, 75, 55])
            self.assertEqual(len(dataset.load_manifest(root)["documents"]), 205)
            self.assertEqual(dataset.ingest(root, inputs, "manual-public-import", "scan", limit=1)["added"], 0)
            for limit in (0, -1, True, dataset.MAX_RUN_DOCUMENTS + 1):
                with self.assertRaisesRegex(ValueError, "count bound"):
                    dataset.ingest(root, inputs, "manual-public-import", "scan", limit=limit)

    def test_indexed_page_groups_match_brute_force_collision_contract(self):
        docs = []
        for index in range(120):
            sha = dataset.digest(f"synthetic asset {index}".encode())
            docs.append(dict(id=sha, sha256=sha, case_key="", duplicate_cluster="", pages=[dict(
                sha256=dataset.digest(f"synthetic page {index}".encode()),
                dhash=hashlib.sha256(f"synthetic bits {index}".encode()).hexdigest()[:16], aspect=1 + (index % 3) * .049)]))
        # Four changed bands leave only the fifth shared; adjacent aspect bins
        # must be searched too. Distance five and aspect .05 are not collisions.
        docs[1]["pages"][0].update(dhash=docs[0]["pages"][0]["dhash"], aspect=1.049)
        bits = int(docs[0]["pages"][0]["dhash"], 16) ^ sum(1 << bit for bit in (0, 13, 26, 39))
        docs[2]["pages"][0].update(dhash=f"{bits:016x}", aspect=1.051)
        docs[3]["case_key"] = docs[4]["case_key"] = "synthetic shared case"
        docs[4]["duplicate_cluster"] = docs[5]["duplicate_cluster"] = "synthetic crop/rephoto provenance"
        docs[6]["pages"][0]["sha256"] = docs[7]["pages"][0]["sha256"]
        expected = [{i} for i in range(len(docs))]
        for i, left in enumerate(docs):
            for j, right in enumerate(docs[:i]):
                collision = (bool(left["case_key"] and left["case_key"] == right["case_key"])
                    or bool(left["duplicate_cluster"] and left["duplicate_cluster"] == right["duplicate_cluster"])
                    or left["sha256"] == right["sha256"] or any(
                        a["sha256"] == b["sha256"] or (abs(a["aspect"] - b["aspect"]) < .05 and
                        (int(a["dhash"], 16) ^ int(b["dhash"], 16)).bit_count() <= 4)
                        for a in left["pages"] for b in right["pages"]))
                if collision:
                    one = next(g for g in expected if i in g)
                    two = next(g for g in expected if j in g)
                    if one is not two:
                        one.update(two); expected.remove(two)
        lookup = {doc["id"]: index for index, doc in enumerate(docs)}
        actual = {frozenset(lookup[doc["id"]] for doc in group) for group in dataset.groups(docs)}
        self.assertEqual(actual, {frozenset(group) for group in expected})

    def test_generation_retains_holdout_and_old_quarantine_can_never_be_new_control(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary); root = self.root(parent)
            manifest = self.independent_manifest(root, parent)
            holdout, development, quarantine = manifest["documents"]
            holdout["case_key"] = self.case_for("holdout")
            development["case_key"] = self.case_for("development")
            quarantine.update(case_key="", case_reviewed=False)
            dataset.write_json(root / "manifest.json", manifest)
            old_split = dataset.freeze(root)
            original_bytes = (root / "manifest.json").read_bytes()
            extended = parent / "generation-two"; extended.mkdir(mode=0o700)
            result = dataset.extend(extended, root)
            self.assertEqual(result["inherited_documents"], 3)
            self.assertFalse((extended / "captures").exists())
            manifest = dataset.load_manifest(extended)
            manifest["documents"][2].update(case_key=self.case_for("holdout") + "-now-reviewed", case_reviewed=True)
            dataset.write_json(extended / "manifest.json", manifest)
            new_split = dataset.freeze(extended)
            self.assertEqual(new_split["assignments"][holdout["id"]], "holdout")
            self.assertEqual(new_split["assignments"][development["id"]], "development")
            self.assertEqual(new_split["assignments"][quarantine["id"]], "development")
            self.assertEqual((root / "manifest.json").read_bytes(), original_bytes)
            self.assertEqual(dataset.validate_split(root, dataset.load_manifest(root)), old_split)

    def test_generation_new_bytes_of_old_case_inherit_split_and_exposure_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary); root = self.root(parent)
            manifest = self.independent_manifest(root, parent, count=1)
            old = manifest["documents"][0]; old["case_key"] = self.case_for("holdout")
            dataset.write_json(root / "manifest.json", manifest); dataset.freeze(root)
            extended = parent / "generation-two"; extended.mkdir(mode=0o700)
            dataset.extend(extended, root)
            inputs = parent / "new-inputs"; inputs.mkdir()
            Image.new("RGB", (30, 40), (30, 70, 110)).save(inputs / "synthetic derivative.png")
            dataset.ingest(extended, inputs, "manual-public-import", "scan")
            updated = dataset.load_manifest(extended)
            new = updated["documents"][1]
            new.update(case_key=old["case_key"], case_reviewed=True, duplicate_reviewed=True)
            dataset.write_json(extended / "manifest.json", updated)
            self.assertEqual(set(dataset.freeze(extended)["assignments"].values()), {"holdout"})
            exposed = parent / "exposed-generation"; exposed.mkdir(mode=0o700)
            dataset.extend(exposed, extended, [new["id"]])
            self.assertEqual(set(dataset.freeze(exposed)["assignments"].values()), {"development"})
            self.assertEqual(set(dataset.validate_split(extended, updated)["assignments"].values()), {"holdout"})

    def test_generation_rejects_inherited_membership_change_and_silent_exposure(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary); root = self.root(parent)
            manifest = self.independent_manifest(root, parent, count=1)
            doc = manifest["documents"][0]; doc["case_key"] = self.case_for("holdout")
            dataset.write_json(root / "manifest.json", manifest); dataset.freeze(root)
            extended = parent / "generation-two"; extended.mkdir(mode=0o700)
            dataset.extend(extended, root)
            changed = dataset.load_manifest(extended)
            changed["documents"][0]["previously_seen"] = True
            dataset.write_json(extended / "manifest.json", changed)
            with self.assertRaisesRegex(ValueError, "exposure/link conflict"):
                dataset.freeze(extended)
            changed["documents"][0]["previously_seen"] = False
            changed["documents"][0]["pages"][0]["dhash"] = "0" * 16
            dataset.write_json(extended / "manifest.json", changed)
            with self.assertRaisesRegex(ValueError, "original/page"):
                dataset.freeze(extended)

    def test_historical_case_aliases_cannot_reenter_control_after_canonical_correction(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary); root = self.root(parent)
            manifest = self.independent_manifest(root, parent, count=1)
            old = manifest["documents"][0]; old["case_key"] = self.case_for("development")
            original_case_key = old["case_key"]
            dataset.write_json(root / "manifest.json", manifest); dataset.freeze(root)
            second = (parent / "generation-two").resolve(); second.mkdir(mode=0o700)
            dataset.extend(second, root)
            manifest = dataset.load_manifest(second)
            manifest["documents"][0]["case_key"] = "synthetic corrected canonical key"
            dataset.write_json(second / "manifest.json", manifest); dataset.freeze(second)
            third = (parent / "generation-three").resolve(); third.mkdir(mode=0o700)
            dataset.extend(third, second)
            inputs = parent / "new-inputs"; inputs.mkdir()
            Image.new("RGB", (21, 32), (55, 66, 77)).save(inputs / "synthetic other act.png")
            dataset.ingest(third, inputs, "manual-public-import", "scan")
            manifest = dataset.load_manifest(third)
            new = manifest["documents"][1]
            new.update(case_key=original_case_key, case_reviewed=True, duplicate_reviewed=True)
            dataset.write_json(third / "manifest.json", manifest)
            self.assertEqual(dataset.freeze(third)["assignments"][new["id"]], "development")

    def test_exposure_closes_over_historical_case_and_duplicate_aliases(self):
        for link in ("case", "cluster"):
            with self.subTest(link=link), tempfile.TemporaryDirectory() as temporary:
                parent = Path(temporary); root = self.root(parent)
                manifest = self.independent_manifest(root, parent, count=2)
                for index, doc in enumerate(manifest["documents"]):
                    doc["case_key"] = self.case_for("holdout") + (str(index) if link == "cluster" else "")
                    if link == "cluster":
                        doc["duplicate_cluster"] = "synthetic historical crop provenance"
                # The grouping's minimum canonical case is deterministically control.
                if link == "cluster":
                    for index, doc in enumerate(manifest["documents"]):
                        doc["case_key"] = self.case_for("holdout") + ("" if index == 0 else "-other")
                dataset.write_json(root / "manifest.json", manifest); dataset.freeze(root)
                second = (parent / "generation-two").resolve(); second.mkdir(mode=0o700)
                dataset.extend(second, root)
                manifest = dataset.load_manifest(second)
                first = manifest["documents"][0]
                if link == "case":
                    first["case_key"] = "synthetic corrected case alias"
                else:
                    first["duplicate_cluster"] = "synthetic corrected crop alias"
                dataset.write_json(second / "manifest.json", manifest)
                self.assertEqual(set(dataset.freeze(second)["assignments"].values()), {"holdout"})
                third = (parent / "generation-three").resolve(); third.mkdir(mode=0o700)
                dataset.extend(third, second, [first["id"]])
                self.assertEqual(set(dataset.freeze(third)["assignments"].values()), {"development"})

    def test_new_link_between_inherited_development_and_holdout_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary); root = self.root(parent)
            manifest = self.independent_manifest(root, parent, count=2)
            manifest["documents"][0]["case_key"] = self.case_for("holdout")
            manifest["documents"][1]["case_key"] = self.case_for("development")
            dataset.write_json(root / "manifest.json", manifest); dataset.freeze(root)
            second = (parent / "generation-two").resolve(); second.mkdir(mode=0o700)
            dataset.extend(second, root)
            manifest = dataset.load_manifest(second)
            for doc in manifest["documents"]:
                doc["duplicate_cluster"] = "synthetic newly found crop link"
            dataset.write_json(second / "manifest.json", manifest)
            with self.assertRaisesRegex(ValueError, "exposure/link conflict"):
                dataset.freeze(second)

    def test_entity_field_and_medium_metrics_keep_gold_coverage_and_owners_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary); root = self.root(parent)
            manifest = self.import_images(root, parent)
            doc = manifest["documents"][0]; doc.update(previously_seen=True, medium="real_photo")
            dataset.write_json(root / "manifest.json", manifest); dataset.freeze(root)
            ref, captured = self.add_score_evidence(root, doc)
            for name, value in (("legal_name", "Synthetic Organization"), ("ogrn", "0000000000000"),
                                ("address", "Synthetic Street 1, apartment 4"), ("doc_date", "01.01.2000")):
                ref["fields"][name].update(state="present", value=value, page=1, participant="organization-1", role="debtor", occurrence="p1-o2")
                captured["observed_pipeline"][name] = {"value": value}
            dataset.write_json(root / "references" / f"{doc['id']}.json", ref)
            dataset.write_json(root / "captures" / f"{doc['id']}.json", captured)
            audit_path = root / "audits" / f"{doc['id']}.json"
            audit = dataset.read_json(audit_path)
            audit.update(reference_sha256=dataset.identity(ref), capture_sha256=dataset.identity(captured))
            for name in ("legal_name", "ogrn", "address", "doc_date"):
                audit["fields"][name].update(owner="correct", source="correct")
            dataset.write_json(audit_path, audit)
            report = dataset.score(root)["partitions"]["development"]
            for name in ("legal_name", "ogrn", "address", "doc_date"):
                metric = report["fields"][name]
                self.assertEqual(metric["reference_coverage"], 1)
                self.assertEqual(metric["exact_value_accuracy"], 1)
                self.assertEqual(metric["qualified_value_owner_source_accuracy"], 1)
                self.assertEqual(report["by_medium"]["real_photo"]["fields"][name], metric)
            self.assertEqual(report["fields"]["fio_raw"]["owner_accuracy"], 0)
            self.assertIsNone(report["fields"]["snils"]["exact_value_accuracy"])
            self.assertIsNone(report["by_medium"]["text_pdf"]["fields"]["legal_name"]["exact_value_accuracy"])

    def test_legacy_references_remain_partial_and_missing_extended_fields_are_unreviewed(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary); root = self.root(parent)
            manifest = self.import_images(root, parent)
            doc = manifest["documents"][0]; doc["previously_seen"] = True
            dataset.write_json(root / "manifest.json", manifest); dataset.freeze(root)
            ref, capture = self.add_score_evidence(root, doc)
            ref["fields"] = {key: ref["fields"][key] for key in dataset.LEGACY_FIELDS}
            dataset.write_json(root / "references" / f"{doc['id']}.json", ref)
            (root / "audits" / f"{doc['id']}.json").unlink()
            report = dataset.score(root)["partitions"]["development"]
            self.assertEqual(report["fields"]["legal_name"]["counts"]["reference_unreviewed"], 1)
            self.assertEqual(report["fields"]["legal_name"]["reference_coverage"], 0)
            self.assertIsNone(report["fields"]["legal_name"]["exact_value_accuracy"])
            self.assertEqual(report["fields"]["fio_raw"]["exact_value_accuracy"], 1)

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
