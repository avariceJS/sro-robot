"""Private capture boundaries and evidence identity, using only synthetic data."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from scripts.dev.quality import dataset_capture as capture


IDENTITY = {"revision": "synthetic", "source_dirty": False, "dirty_sha256": "synthetic"}
TEXT = "Должник: Иванов Иван Иванович. "


class DatasetCaptureTests(unittest.TestCase):
    def test_appointment_hearing_rescan_is_single_full_frame_and_reuses_budget(self):
        from sro_robot.core import ocr

        primary = ('ОПРЕДЕЛЕНИЕ\nДело № А40-123/2026\nо банкротстве\nУСТАНОВИЛ:\n'
                   'Заявление принято.\nОПРЕДЕЛИЛ:\nНазначить судебное заседание на 10 часов 30 минут.')
        budget = ocr.OcrBudget()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.jpg"
            with Image.new("RGB", (80, 60), "white") as source:
                exif = source.getexif()
                exif[274] = 6
                source.save(path, exif=exif)
            with patch.object(ocr, "_tesseract", return_value="\n result \n") as read:
                self.assertEqual(capture.capture_appointment_hearing(path, [primary], budget), ["result"])
                read.assert_called_once()
                frame, config, used_budget = read.call_args.args
                self.assertEqual((frame.mode, frame.size, config), ("L", (60, 80), "--psm 4"))
                self.assertIs(used_budget, budget)
                with self.assertRaises(ValueError):
                    frame.getpixel((0, 0))
            complete = primary.replace("на 10 часов", "на 20.01.2026 в 10 часов")
            with patch.object(ocr, "_tesseract") as read:
                self.assertEqual(capture.capture_appointment_hearing(path, [complete], budget), [])
                self.assertEqual(capture.capture_appointment_hearing(path, ["synthetic"], budget), [])
                self.assertEqual(capture.capture_appointment_hearing(path.with_suffix(".pdf"), [primary], budget), [])
                read.assert_not_called()
            with patch.object(ocr, "_tesseract", side_effect=ocr.OcrProcessingTimeout("Tesseract OCR")):
                self.assertEqual(capture.capture_appointment_hearing(path, [primary], budget), [])

    def test_appointment_caption_uses_oriented_grayscale_crop_and_filters_blank(self):
        from sro_robot.core import ocr

        seen = []

        def read(image, config, budget):
            seen.append((config, budget, image.mode, image.size, image))
            return "\n caption \f" if config == "--psm 6" else "\n\f"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.jpg"
            with Image.new("RGB", (80, 60), "white") as source:
                exif = source.getexif()
                exif[274] = 6
                source.save(path, exif=exif)
            with patch.object(ocr, "_tesseract", side_effect=read):
                self.assertEqual(capture.capture_appointment_caption(path), ["caption"])

        self.assertEqual([(row[0], row[2], row[3]) for row in seen], [
            ("--psm 6", "L", (60, 48)), ("--psm 11", "L", (60, 48)),
        ])
        self.assertIs(seen[0][1], seen[1][1])
        self.assertEqual(seen[0][1].tesseract_timeout_seconds, 20)
        with self.assertRaises(ValueError):
            seen[0][4].getpixel((0, 0))

    def test_appointment_caption_enforces_photo_suffix_and_pixel_bound(self):
        from sro_robot.core import ocr

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.png"
            with Image.new("L", (12, 12)) as source:
                source.save(path)
            with patch.object(ocr, "DEFAULT_MAX_IMAGE_PIXELS", 100), patch.object(ocr, "_tesseract") as read:
                with self.assertRaises(ocr.ImagePixelLimitExceeded):
                    capture.capture_appointment_caption(path)
                with self.assertRaisesRegex(ValueError, "appointment_caption_requires_photo"):
                    capture.capture_appointment_caption(path.with_suffix(".pdf"))
                read.assert_not_called()

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
            return capture.capture_document(path, **kwargs)

    def test_opt_in_caption_capture_records_same_original_reads_and_shared_budget(self):
        import pytesseract
        from sro_robot.core import extract, ocr

        primary = ('ОПРЕДЕЛЕНИЕ\nДело № А99-123/2099\nо банкротстве\nОПРЕДЕЛИЛ:\n'
                   'Назначить судебное заседание на 25.11.2099 в 10:30.')
        caption = 'ОПРЕДЕЛЕНИЕ\nДело № А99-123/2099\nСудья Макетов А. Б.\nо банкротстве'
        seen = []

        def read(image, config, budget):
            pytesseract.pytesseract.run_tesseract()
            seen.append((image, config, budget))
            return primary if config == '--psm 3' else '\n' + caption + '\n\f'

        def parse_file(path, *, budget):
            budget.ocr_pages.add(1)
            ocr._tesseract(type('SyntheticImage', (), {'size': (60, 80)})(), '--psm 3', budget)
            return extract.parse([primary])

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'synthetic.png'
            with Image.new('RGB', (60, 80), 'white') as image:
                image.save(path)
            with patch.object(pytesseract.pytesseract, 'run_tesseract'), patch.object(ocr, '_tesseract', side_effect=read):
                result = self.run_capture(path, parse_file, appointment_photo_caption=True)
        self.assertEqual(result['variants'], [primary])
        self.assertEqual(result['appointment_caption_readings'], [caption, caption])
        self.assertEqual(result['appointment_caption_source_sha256'], result['input_sha256'])
        self.assertEqual(result['capture']['protocol'], capture.CAPTION_RESCAN_PROTOCOL)
        self.assertEqual(result['capture']['completed_ocr'], 3)
        self.assertEqual([item['config'] for item in result['readings']], ['--psm 3', '--psm 6', '--psm 11'])
        for item in result['readings'][-2:]:
            self.assertEqual(item['purpose'], 'appointment-caption')
            self.assertEqual(item['crop'], 'top-three-fifths')
            self.assertEqual(item['transform'], 'grayscale-autocontrast')
            self.assertEqual(item['size'], [60, 48])
            self.assertEqual(item['text'].strip(), caption)
        self.assertTrue(all(item[2] is seen[0][2] for item in seen))
        self.assertEqual(seen[0][2].deadline_seconds, 50)
        for image, _config, _budget in seen[1:]:
            with self.assertRaises(ValueError):
                image.getpixel((0, 0))

    def test_optional_caption_failures_preserve_primary_and_any_completed_crop(self):
        import pytesseract
        from sro_robot.core import extract, ocr

        primary = 'ОПРЕДЕЛЕНИЕ\nДело № А99-123/2099\nо банкротстве'

        def parse_file(path, *, budget):
            budget.ocr_pages.add(1)
            ocr._tesseract(type('SyntheticImage', (), {'size': (60, 80)})(), '--psm 3', budget)
            return extract.parse([primary])

        for failed_config, error in (('--psm 6', ocr.OcrProcessingTimeout('overall OCR')),
                                     ('--psm 11', ocr.OcrProcessingTimeout('Tesseract OCR')),
                                     ('--psm 11', pytesseract.TesseractError(1, 'synthetic failure'))):
            def read(image, config, budget):
                if config == failed_config:
                    raise error
                pytesseract.pytesseract.run_tesseract()
                return primary

            with self.subTest(failed_config=failed_config, error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'synthetic.png'
                with Image.new('L', (60, 80), 'white') as image:
                    image.save(path)
                with patch.object(pytesseract.pytesseract, 'run_tesseract'), patch.object(ocr, '_tesseract', side_effect=read):
                    result = self.run_capture(path, parse_file, appointment_photo_caption=True)
                self.assertEqual(result['variants'], [primary])
                self.assertEqual(result['capture']['completed_ocr'], 1 if failed_config == '--psm 6' else 2)
                self.assertEqual(result.get('appointment_caption_readings', []), [] if failed_config == '--psm 6' else [primary])
                self.assertEqual([row['config'] for row in result['readings']],
                                 ['--psm 3'] if failed_config == '--psm 6' else ['--psm 3', '--psm 6'])

    def test_automatic_caption_trigger_requires_single_case_and_missing_applicant_and_judge(self):
        from sro_robot.core import ocr

        primary = 'ОПРЕДЕЛЕНИЕ\nДело № А99-123/2099\nо банкротстве'
        for variants in ([primary + '\nЗаявитель: Макетова Анна Петровна'],
                         [primary + '\nСудья Макетов А. Б.'],
                         [primary, primary.replace('А99-123', 'А99-456')], ['о банкротстве']):
            with self.subTest(variants=variants), patch.object(ocr, 'load_pages') as load:
                self.assertEqual(capture.capture_appointment_caption(Path('synthetic.jpg'), variants=variants), [])
                load.assert_not_called()
        budget = ocr.OcrBudget()
        with patch.object(budget, 'timeout', side_effect=ocr.OcrProcessingTimeout('overall OCR')), \
                patch.object(ocr, 'load_pages') as load:
            self.assertEqual(capture.capture_appointment_caption(Path('synthetic.jpg'), budget, variants=[primary]), [])
            load.assert_not_called()

    def test_caption_mode_is_explicit_mutually_exclusive_and_photo_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'synthetic.png'; source.write_bytes(b'synthetic')
            output = root / 'output.json'
            with patch.object(capture, 'capture_document', return_value={'variants': ['synthetic']}) as worker:
                status = capture.main(['--input', str(source), '--output', str(output), '--private-root', str(root),
                                       '--appointment-photo-caption'])
                self.assertEqual(status, 0)
                worker.assert_called_once_with(source.resolve(), appointment_photo_caption=True)
            with self.assertRaisesRegex(ValueError, 'modes_conflict'):
                capture.capture_document(source, appointment_photo_caption=True, appointment_photo_hearing=True)
            with self.assertRaisesRegex(ValueError, 'requires_photo'):
                capture.capture_document(source.with_suffix('.pdf'), appointment_photo_caption=True)
            with patch.object(capture, 'capture_document') as worker, redirect_stderr(io.StringIO()):
                status = capture.main(['--input', str(source), '--output', str(output), '--private-root', str(root),
                                       '--appointment-photo-caption', '--appointment-photo-hearing'])
                self.assertEqual(status, 1)
                worker.assert_not_called()

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
        self.assertEqual(result['capture']['protocol'], capture.STANDARD_PROTOCOL)
        self.assertNotIn('appointment_caption_readings', result)
        self.assertNotIn('appointment_caption_source_sha256', result)
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

    def test_optional_caption_input_change_cannot_claim_same_original(self):
        import pytesseract
        from sro_robot.core import extract

        def parse_file(path, *, budget):
            budget.ocr_pages.add(1)
            pytesseract.pytesseract.run_tesseract()
            return extract.parse([TEXT])

        def changed_caption(path, **kwargs):
            path.write_bytes(b'changed synthetic original during optional caption')
            return ['synthetic caption']

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'synthetic.png'; path.write_bytes(b'synthetic original')
            with patch.object(pytesseract.pytesseract, 'run_tesseract'), \
                    patch.object(capture, 'capture_appointment_caption', side_effect=changed_caption):
                with self.assertRaisesRegex(ValueError, 'input_or_source_changed'):
                    self.run_capture(path, parse_file, appointment_photo_caption=True)


if __name__ == "__main__":
    unittest.main()
