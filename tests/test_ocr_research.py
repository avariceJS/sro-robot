"""Synthetic-only checks for private weak-reference raster OCR research."""
import contextlib
import io
import logging
import json
from types import SimpleNamespace
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw
from scripts.dev.quality import ocr_research as research


class OcrResearchTests(unittest.TestCase):
    def test_exact_distance_matches_independent_matrix(self):
        def matrix(a, b):
            rows = [list(range(len(b) + 1))]
            for i, ca in enumerate(a, 1):
                rows.append([i])
                for j, cb in enumerate(b, 1):
                    rows[-1].append(min(rows[-2][j] + 1, rows[-1][j - 1] + 1,
                                        rows[-2][j - 1] + (ca != cb)))
            return rows[-1][-1]
        randomizer = random.Random(304)
        for _ in range(300):
            a = [randomizer.choice("abc") for _ in range(randomizer.randrange(30))]
            b = [randomizer.choice("abc") for _ in range(randomizer.randrange(30))]
            self.assertEqual(research.edit_distance(a, b), matrix(a, b))
        # Python integers must retain the bit-vector recurrence beyond the
        # 64-bit width of a machine word on real multi-thousand-character pages.
        for a, b in (("a" * 300, "a" * 299 + "b"), ("abc " * 75, "abc " * 74 + "xyz"),
                     (["word"] * 130, ["word"] * 128 + ["other", "word"])):
            self.assertEqual(research.edit_distance(a, b), matrix(a, b))

    def test_metrics_keep_digits_letters_lines_and_missing_reading(self):
        result = research.measure("АБ 123\nВГ 45", "АВ 124\nВГ 45")
        self.assertEqual(result["characters"]["edits"], 2)
        self.assertEqual(result["digits"]["edits"], 1)
        self.assertEqual(result["letters"]["edits"], 1)
        self.assertEqual(result["lines"]["edits"], 1)
        missing = research.measure("abc 12", "")
        self.assertEqual(missing["characters"]["edits"], 6)
        self.assertIsNone(research.aggregate([]).get("strata", {}).get("clean"))

    def test_blank_or_misaligned_raster_never_qualifies(self):
        boxes = [(10, 10, 30, 30)] * 30
        white = Image.new("RGB", (100, 100), "white")
        result = research.qualify("Synthetic reference words " * 20, boxes, (100, 100), white)
        self.assertFalse(result["eligible"])
        self.assertIn("weak_visible_alignment", result["reasons"])
        ImageDraw.Draw(white).rectangle((12, 12, 14, 28), fill="black")
        accepted = research.qualify("Synthetic reference words " * 20, boxes, (100, 100), white)
        self.assertTrue(accepted["eligible"])
        self.assertFalse(accepted["human_verified"])

    def test_invalid_geometry_and_garbled_text_rejected(self):
        image = Image.new("RGB", (100, 100), "white")
        result = research.qualify("x\ufffd" * 150, [(0, 0, 999, 100)] * 30, (100, 100), image)
        self.assertFalse(result["eligible"])
        self.assertIn("invalid_geometry", result["reasons"])
        self.assertIn("unusable_text", result["reasons"])

    def test_distortions_are_deterministic_and_distinct(self):
        image = Image.new("RGB", (200, 200), "white")
        ImageDraw.Draw(image).rectangle((20, 30, 100, 40), fill="black")
        for variant in research.VARIANTS:
            first = research.distort(image, variant)
            second = research.distort(image, variant)
            self.assertEqual(first.tobytes(), second.tobytes())
            if variant != "clean":
                self.assertNotEqual(first.tobytes(), image.tobytes())

    def test_aggregate_never_leaks_text_or_qualifies_as_real_photo(self):
        private = "PRIVATE SENTINEL"
        result = research.aggregate([{"reference": private, "input_sha256": private,
            "qualification": {"eligible": True}, "readings": [{"variant": "clean", "text": private,
                "seconds": 1., "metrics": research.measure("abc 12", "abc 13")}]}])
        self.assertNotIn(private, str(result))
        self.assertEqual(result["strata"]["clean"]["metrics"]["characters"]["rate"], 1 / 6)
        self.assertEqual(result["real_photo_pages"], 0)
        self.assertFalse(result["population_accuracy_qualified"])

    def test_xml_page_slots_and_order_are_explicit(self):
        xml = '<html><page width="100" height="200"><line><word xMin="1" yMin="2" xMax="9" yMax="8">abc</word></line></page></html>'
        text, boxes, geometry = research.bbox_reference(xml)
        self.assertEqual(text, "abc")
        self.assertEqual(boxes, [(1., 2., 9., 8.)])
        self.assertEqual(geometry, (100., 200.))
        with self.assertRaises(ValueError):
            research.bbox_reference('<html><page/><page/></html>')

    def test_pair_counts_and_duplicate_rasters_are_explicit(self):
        def reading(recognizer, observed):
            return {"variant": "shadow", "recognizer": recognizer, "seconds": .2,
                    "metrics": research.measure("abc 123", observed)}
        record = {"raster_sha256": "synthetic-raster", "qualification": {"eligible": True},
                  "readings": [reading("baseline", "ab 12"), reading("sauvola", "abc 123")]}
        result = research.aggregate([record, record])
        self.assertEqual(result["duplicate_raster_pages"], 1)
        self.assertEqual(result["strata"]["shadow"]["pages"], 1)
        self.assertEqual(result["paired_sauvola"]["shadow"]["improved_pages"], 1)
        self.assertEqual(result["paired_sauvola"]["shadow"]["candidate_edits"], 0)
        self.assertIsNone(result["independent_cases"])

    def test_resume_uses_frozen_inputs_while_acquisition_grows(self):
        from scripts.dev.quality.court_dataset import private_root
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"; source.mkdir()
            root = private_root(base / "private")
            (source / "one.pdf").write_bytes(b"synthetic-original-one")
            (source / "duplicate.pdf").write_bytes(b"synthetic-original-one")
            args = SimpleNamespace(input_dir=source, output_name="test", limit_documents=200,
                                   pages_per_document=1, dpi=200, batch_seconds=30,
                                   recognizers="baseline,sauvola", exclude_run=[])
            calls = []
            def worker(command, **kwargs):
                from scripts.dev.quality.dataset_capture import digest
                path = Path(command[command.index("--input") + 1])
                output = Path(command[command.index("--worker-output") + 1])
                result = {"schema": research.SCHEMA, "input_sha256": digest(path), "page": 1, "fingerprint": {"test": 1},
                          "qualification": {"eligible": False, "reasons": ["unusable_text"],
                                            "reference_kind": "embedded-text-weak", "human_verified": False},
                          "readings": []}
                result["payload_sha256"] = research.record_digest(result)
                output.write_text(json.dumps(result))
                calls.append(command)
            with patch.object(research, "fingerprint", return_value={"test": 1}), \
                    patch("scripts.dev.tesseract_native.resources", return_value=(90, 0, 0)), \
                    patch("scripts.dev.tesseract_native.run_owned", side_effect=worker), \
                    patch.object(research.subprocess, "run", return_value=SimpleNamespace(stdout=b"Pages: 1\n")):
                first = research.run_batch(args, root)
                self.assertEqual(first["completed_this_batch"], 1)
                self.assertEqual(first["excluded_input_files"], 1)
                (source / "two.pdf").write_bytes(b"synthetic-original-two")
                resumed = research.run_batch(args, root)
                self.assertEqual(resumed["completed_this_batch"], 0)
                self.assertEqual(resumed["selected_documents"], 1)
                self.assertEqual(len(calls), 1)
                args.output_name = "new"
                args.exclude_run = ["test"]
                second = research.run_batch(args, root)
                self.assertEqual(second["selected_documents"], 1)
                self.assertEqual(second["excluded_input_files"], 2)
                self.assertEqual(second["completed_this_batch"], 1)
                # Source volume is not a final dataset cap: choose a bounded
                # new cohort from an acquisition directory larger than 200.
                for index in range(201):
                    (source / f"extra-{index}.pdf").write_bytes(f"synthetic-extra-{index}".encode())
                args.output_name = "bounded"
                args.exclude_run = ["test", "new"]
                args.limit_documents = 2
                third = research.run_batch(args, root)
                self.assertEqual(third["selected_documents"], 2)
                self.assertEqual(third["completed_this_batch"], 2)
                checkpoint = next((root / "bounded").glob("*-1.json"))
                corrupted = json.loads(checkpoint.read_text())
                corrupted["input_sha256"] = "unrelated"
                checkpoint.write_text(json.dumps(corrupted))
                with self.assertRaisesRegex(ValueError, "checkpoint_identity"):
                    research.run_batch(args, root)
                args.output_name = "new"
                args.exclude_run = ["test"]
                args.limit_documents = 200
                (source / "two.pdf").write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "frozen_input_changed"):
                    research.run_batch(args, root)

    def test_source_directory_cannot_scan_checkout_or_its_ancestors(self):
        for source in (research.ROOT, research.ROOT.parent):
            args = SimpleNamespace(input_dir=source)
            with self.assertRaisesRegex(ValueError, "source_boundary"):
                research.run_batch(args, source)

    def test_raster_work_is_admitted_before_render(self):
        xml = b'<html><page width="14000" height="14000"><line/></page></html>'
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "synthetic.pdf"
            source.write_bytes(b"synthetic")
            def text_layer(command, **kwargs):
                Path(command[-1]).write_bytes(xml)
            with patch.object(research.subprocess, "run", side_effect=text_layer), \
                    patch("pdf2image.convert_from_path") as render:
                with self.assertRaisesRegex(ValueError, "raster_bound"):
                    research.capture_page(source, 1, 200, research.VARIANTS)
                render.assert_not_called()

    def test_cli_errors_are_fixed_token_and_restore_logging(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(research, "run_batch", side_effect=ValueError("PRIVATE SENTINEL")), \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                previous = logging.root.manager.disable
                logging.disable(logging.ERROR)
                try:
                    self.assertEqual(research.main(["--private-root", str(Path(temporary) / "private")]), 1)
                    self.assertEqual(logging.root.manager.disable, logging.ERROR)
                finally:
                    logging.disable(previous)
                self.assertEqual(stderr.getvalue(), "ocr_research_failed\n")


if __name__ == "__main__":
    unittest.main()
