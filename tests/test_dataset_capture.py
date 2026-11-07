"""Private capture boundaries and evidence identity, using only synthetic data."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.dev.quality import dataset_capture as capture


IDENTITY = {"revision": "synthetic", "source_dirty": False, "dirty_sha256": "synthetic"}
TEXT = "Должник: Иванов Иван Иванович. "


class DatasetCaptureTests(unittest.TestCase):
    def test_cli_restores_caller_logging_after_success_and_failure(self):
        prior = logging.root.manager.disable
        try:
            logging.disable(logging.WARNING)
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); source = root / "source.png"; source.write_bytes(b"synthetic")
                for failure in (False, True):
                    with self.subTest(failure=failure), patch.object(
                        capture, "capture_document", side_effect=RuntimeError("synthetic") if failure else None,
                        return_value={"variants": ["synthetic"]},
                    ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        result = capture.main(["--input", str(source), "--output", str(root / "result.json"), "--private-root", str(root)])
                    self.assertEqual(result, 1 if failure else 0)
                    self.assertEqual(logging.root.manager.disable, logging.WARNING)
                    logger = logging.getLogger("synthetic.capture.caller")
                    with self.assertLogs(logger, "ERROR"):
                        logger.error("synthetic caller error")
        finally:
            logging.disable(prior)

    def run_capture(self, path, fake_parse, **kwargs):
        from sro_robot.core import extract
        with patch.object(extract, "parse_file", side_effect=fake_parse), patch.object(
            capture, "source_identity", return_value=IDENTITY,
        ), patch.object(capture, "runtime_identity", return_value={"synthetic": True}):
            return capture.capture_document(path)

    def test_photo_keeps_accepted_parser_and_primary_text_not_rejected_attempt(self):
        import pytesseract
        from sro_robot.core import extract, ocr
        parse = extract.parse
        def parse_file(path, *, budget):
            self.assertEqual(budget.deadline_seconds, 50)
            self.assertEqual(budget.tesseract_timeout_seconds, 20)
            budget.ocr_pages.add(1)
            budget.recovery_reasons.append("synthetic-recovery")
            pytesseract.pytesseract.run_tesseract()
            ocr._tesseract(type("SyntheticImage", (), {"size": (100, 200)})(), "--psm 3", budget)
            selected = extract.parse([TEXT])
            extract.parse(["Должник: Петров Петр Петрович"])
            return selected
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.png"; path.write_bytes(b"synthetic")
            with patch.object(pytesseract.pytesseract, "run_tesseract", return_value=None), patch.object(
                ocr, "_tesseract", return_value=TEXT,
            ):
                result = self.run_capture(path, parse_file)
        self.assertIs(extract.parse, parse)
        self.assertEqual(result["variants"], [TEXT])
        self.assertEqual(result["primary_pages"], [{"page": 1, "text": TEXT, "origin": "ocr"}])
        self.assertEqual(result["capture"]["completed_ocr"], 1)
        self.assertEqual(result["capture"]["parser_attempts"], 2)
        self.assertEqual(result["capture"]["reference"], "unverified")
        self.assertEqual(result["readings"][0]["text"], TEXT)
        self.assertNotIn("raw_text", result["observed_pipeline"])

    def test_fast_text_pdf_zero_calls_remains_weak_and_keeps_page_positions(self):
        from sro_robot.core import extract
        text = TEXT * 20 + "\f" + TEXT * 20 + "\f"
        def parse_file(path, *, budget):
            # Actual fast PDF path does not populate text_layer_pages.
            return extract.parse([text])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.pdf"; path.write_bytes(b"synthetic")
            result = self.run_capture(path, parse_file)
        self.assertEqual(result["capture"]["completed_ocr"], 0)
        self.assertEqual(result["capture"]["text_layer_pages"], [1, 2])
        self.assertEqual(len(result["primary_pages"]), 2)
        self.assertEqual({p["origin"] for p in result["primary_pages"]}, {"embedded-text-weak"})

    def test_pdf_blank_position_is_preserved(self):
        from sro_robot.core import extract
        text = TEXT * 5 + "\f\f" + TEXT * 5
        def parse_file(path, *, budget):
            budget.text_layer_pages.update({1, 3})
            return extract.parse([text])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.pdf"; path.write_bytes(b"synthetic")
            result = self.run_capture(path, parse_file)
        self.assertEqual(result["primary_pages"][1], {"page": 2, "text": "", "origin": "unknown"})

    def test_raster_cannot_claim_success_without_completed_ocr(self):
        from sro_robot.core import extract
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.png"; path.write_bytes(b"synthetic")
            with self.assertRaisesRegex(ValueError, "liveness"):
                self.run_capture(path, lambda path, **kwargs: extract.parse([TEXT]))

    def test_failure_restores_all_monkeypatches(self):
        from sro_robot.core import extract, ocr
        import pytesseract
        originals = (extract.parse, ocr._tesseract, pytesseract.pytesseract.run_tesseract)
        def failed(*args, **kwargs):
            raise RuntimeError("synthetic failure")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.png"; path.write_bytes(b"synthetic")
            with self.assertRaises(RuntimeError):
                self.run_capture(path, failed)
        self.assertEqual(originals, (extract.parse, ocr._tesseract, pytesseract.pytesseract.run_tesseract))

    def test_private_cli_is_atomic_mode600_and_silent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / "source.png"; source.write_bytes(b"synthetic")
            output = root / "captures/result.json"
            stdout, stderr = io.StringIO(), io.StringIO()
            def noisy(_):
                print("private synthetic text")
                return {"variants": ["private synthetic text"]}
            with patch.object(capture, "capture_document", side_effect=noisy), redirect_stdout(stdout), redirect_stderr(stderr):
                status = capture.main(["--input", str(source), "--output", str(output), "--private-root", str(root)])
            self.assertEqual(status, 0)
            self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(output.read_text())["variants"], ["private synthetic text"])
            self.assertEqual(list(output.parent.iterdir()), [output])

    def test_cli_failure_is_fixed_token_and_preserves_previous_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / "source.png"; source.write_bytes(b"synthetic")
            output = root / "result.json"; output.write_text("prior synthetic capture")
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(capture, "capture_document", side_effect=ValueError("PRIVATE SYNTHETIC")), redirect_stdout(stdout), redirect_stderr(stderr):
                status = capture.main(["--input", str(source), "--output", str(output), "--private-root", str(root)])
            self.assertEqual(status, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "dataset_capture_failed\n")
            self.assertEqual(output.read_text(), "prior synthetic capture")

    def test_invalid_arguments_never_echo_arbitrary_values(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = capture.main(["--unexpected", "PRIVATE SYNTHETIC"])
        self.assertEqual(status, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "dataset_capture_failed\n")

    def test_cli_rejects_output_and_input_symlink_escape(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as other:
            root = Path(temporary); source = root / "source.png"; source.write_bytes(b"synthetic")
            (root / "escape").symlink_to(other, target_is_directory=True)
            stderr = io.StringIO()
            with patch.object(capture, "capture_document") as worker, redirect_stderr(stderr):
                status = capture.main(["--input", str(source), "--output", str(root / "escape/out.json"), "--private-root", str(root)])
            self.assertEqual(status, 1)
            worker.assert_not_called()

    def test_source_or_input_change_aborts_capture(self):
        import pytesseract
        from sro_robot.core import extract
        def changed(path, *, budget):
            budget.ocr_pages.add(1)
            pytesseract.pytesseract.run_tesseract()
            path.write_bytes(b"changed synthetic input")
            return extract.parse([TEXT])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.png"; path.write_bytes(b"synthetic")
            with patch.object(pytesseract.pytesseract, "run_tesseract", return_value=None):
                with self.assertRaisesRegex(ValueError, "changed"):
                    self.run_capture(path, changed)


if __name__ == "__main__":
    unittest.main()
