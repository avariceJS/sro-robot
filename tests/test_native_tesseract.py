"""Host/artifact selection boundaries; synthetic files, no models or OCR."""
from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import pytesseract
from sro_robot.core import native_tesseract as native


class NativeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "tessdata"; self.data.mkdir()
        for name in ("rus", "osd"):
            (self.data / (name + ".traineddata")).write_bytes(b"synthetic-" + name.encode())
        self.system = self.root / "system/bin/tesseract"; self.system.parent.mkdir(parents=True)
        self.system.write_bytes(b"system")
        self.system_library = self.root / "system/lib/libtesseract.5.dylib"
        self.system_library.parent.mkdir(); self.system_library.write_bytes(b"system-engine")
        self.lept = self.root / "leptonica"; self.lept.write_bytes(b"decoder")
        for mock in (patch.dict(os.environ, {}, clear=True),
                     patch.object(native, "supported_host", return_value=True),
                     patch.object(native.shutil, "which", return_value=str(self.system)),
                     patch.object(native.subprocess, "check_output", return_value="tesseract 5.5.3\n leptonica-1.87.0\n")):
            mock.start(); self.addCleanup(mock.stop)
        binary = self.root / "candidate"; binary.write_bytes(b"native-executable")
        digest = native.sha256(binary); directory = self.root / digest; directory.mkdir()
        self.command = directory / "tesseract"; binary.rename(self.command)
        self.manifest = dict(policy=native.POLICY, source_commit=native.SOURCE_COMMIT,
                             qualified=True, binary_sha256=digest,
                             identity=native.host_identity(self.data, self.lept))
        self.publish()

    def publish(self):
        (self.root / "qualified.json").write_text(json.dumps(self.manifest))

    def selected(self):
        return native.qualified_command(self.root)

    def test_complete_artifact_is_selected_after_load_probe(self):
        self.assertEqual(self.selected(), str(self.command))
        native.subprocess.check_output.assert_called_once_with(
            [str(self.command), "--version"], stderr=subprocess.STDOUT, timeout=1, text=True)

    def test_unsupported_host_and_explicit_optout_never_probe_artifact(self):
        with patch.object(native, "supported_host", return_value=False):
            self.assertIsNone(self.selected())
        with patch.dict(os.environ, {"SRO_TESSERACT_NATIVE": "0"}):
            self.assertIsNone(self.selected())
        native.subprocess.check_output.assert_not_called()

    def test_incomplete_stale_or_unqualified_manifest_is_rejected(self):
        for key, bad in (("policy", "old"), ("source_commit", "other"),
                         ("qualified", False), ("qualified", 1),
                         ("binary_sha256", "../../unsafe")):
            with self.subTest(key=key, bad=bad):
                original = self.manifest[key]; self.manifest[key] = bad; self.publish()
                self.assertIsNone(self.selected())
                self.manifest[key] = original
        (self.root / "qualified.json").write_text("{")
        self.assertIsNone(self.selected())
        (self.root / "qualified.json").unlink()
        self.assertIsNone(self.selected())

    def test_changed_binary_system_model_decoder_or_patch_requires_requalification(self):
        for path in (self.command, self.system, self.system_library, self.data / "rus.traineddata", self.lept):
            with self.subTest(path=path.name):
                original = path.read_bytes(); path.write_bytes(original + b"change")
                self.assertIsNone(self.selected()); path.write_bytes(original)
        for key in ("patch_sha256", "png_patch_sha256"):
            original = self.manifest["identity"][key]
            self.manifest["identity"][key] = "0" * 64; self.publish()
            self.assertIsNone(self.selected())
            self.manifest["identity"][key] = original

    def test_changed_path_or_effective_model_directory_is_rejected(self):
        another = self.root / "another-tesseract"; another.write_bytes(self.system.read_bytes())
        with patch.object(native.shutil, "which", return_value=str(another)):
            self.assertIsNone(self.selected())
        with patch.dict(os.environ, {"TESSDATA_PREFIX": str(self.root / "other-models")}):
            self.assertIsNone(self.selected())
        with patch.dict(os.environ, {"TESSDATA_PREFIX": str(self.data)}):
            self.assertEqual(self.selected(), str(self.command))
        (self.data / "eng.traineddata").write_bytes(b"new-language")
        self.assertEqual(self.selected(), str(self.command))
        self.assertEqual(set(self.manifest["identity"]["models"]), {"rus.traineddata", "osd.traineddata"})

    def test_changed_text_output_configuration_is_rejected(self):
        configs = self.data / "configs"; configs.mkdir()
        (configs / "tsv").write_bytes(b"synthetic-config")
        self.assertIsNone(self.selected())

    def test_retargeted_decoder_link_or_custom_loader_environment_is_rejected(self):
        other = self.root / "other-leptonica"; other.write_bytes(self.lept.read_bytes())
        link = self.root / "active-leptonica"; link.symlink_to(self.lept)
        self.manifest["identity"] = native.host_identity(self.data, link); self.publish()
        self.assertEqual(self.selected(), str(self.command))
        link.unlink(); link.symlink_to(other)
        self.assertIsNone(self.selected())
        with patch.dict(os.environ, {"DYLD_LIBRARY_PATH": "/synthetic"}):
            self.assertIsNone(self.selected())

    def test_retargeted_model_directory_is_rejected_even_with_identical_weights(self):
        other = self.root / "other-models"; other.mkdir()
        for path in self.data.iterdir():
            (other / path.name).write_bytes(path.read_bytes())
        link = self.root / "model-lookup"; link.symlink_to(self.data, target_is_directory=True)
        self.manifest["identity"] = native.host_identity(link, self.lept); self.publish()
        self.assertEqual(self.selected(), str(self.command))
        link.unlink(); link.symlink_to(other, target_is_directory=True)
        self.assertIsNone(self.selected())

    def test_load_failure_or_timeout_falls_back_before_recognition(self):
        for failure in (OSError("dependency unavailable"), subprocess.TimeoutExpired("version", 1)):
            with patch.object(native.subprocess, "check_output", side_effect=failure):
                self.assertIsNone(self.selected())

    def test_relocated_candidate_can_be_checked_before_atomic_publication(self):
        old = (self.root / "qualified.json").read_bytes()
        with patch.object(native.subprocess, "check_output", side_effect=OSError()):
            self.assertIsNone(native.qualified_command(self.root, manifest=self.manifest))
        self.assertEqual((self.root / "qualified.json").read_bytes(), old)


class CommandOwnershipTests(unittest.TestCase):
    def test_custom_command_is_preserved_without_validation(self):
        with patch.object(pytesseract.pytesseract, "tesseract_cmd", "/custom/tesseract"), patch.object(native, "_owned_command", None), patch.object(native, "qualified_command") as select:
            self.assertFalse(native.configure_native_tesseract())
            self.assertEqual(pytesseract.pytesseract.tesseract_cmd, "/custom/tesseract")
            select.assert_not_called()

    def test_own_selection_is_revalidated_and_stale_selection_resets(self):
        with patch.object(pytesseract.pytesseract, "tesseract_cmd", "tesseract"), patch.object(native, "_owned_command", None), patch.object(native, "qualified_command", side_effect=["/qualified/tesseract", "/qualified/tesseract", None]) as select:
            self.assertTrue(native.configure_native_tesseract())
            self.assertTrue(native.configure_native_tesseract())
            self.assertFalse(native.configure_native_tesseract())
            self.assertEqual(pytesseract.pytesseract.tesseract_cmd, "tesseract")
            self.assertIsNone(native._owned_command)
            self.assertEqual(select.call_count, 3)


class NativeBudgetTests(unittest.TestCase):
    def test_preflight_is_shared_by_text_coordinates_and_orientation_calls(self):
        from PIL import Image
        from sro_robot.core import ocr
        budget = ocr.OcrBudget(allow_native_tesseract=True)
        def outputs(input_filename, output_base, *a, **kw):
            Path(output_base + ".txt").write_text("synthetic")
            Path(output_base + ".tsv").write_text("level\ttext\n5\tsynthetic\n")
        with (
            Image.new("L", (1600, 1600), 255) as image,
            patch.object(native, "configure_native_tesseract", return_value=False) as configure,
            patch.object(ocr.pytesseract, "image_to_string", return_value="synthetic"),
            patch.object(ocr.pytesseract, "image_to_osd", return_value="Rotate: 90\nOrientation confidence: 25"),
            patch.object(ocr.pytesseract.pytesseract, "run_tesseract", side_effect=outputs),
        ):
            self.assertEqual(ocr._tesseract(image, "--psm 3", budget), "synthetic")
            self.assertEqual(ocr._photo_rotation(image, budget), 90)
            self.assertEqual(ocr._tesseract_with_source(image, "--psm 6", budget)[0], "synthetic")
        configure.assert_called_once_with()

    def test_disabled_or_expired_budget_never_starts_model_preflight(self):
        from sro_robot.core import ocr
        with patch.object(native, "configure_native_tesseract") as configure:
            ocr._prepare_tesseract(ocr.OcrBudget())
            expired = ocr.OcrBudget(allow_native_tesseract=True)
            expired._deadline = 0
            with self.assertRaises(ocr.OcrProcessingTimeout):
                ocr._prepare_tesseract(expired)
        configure.assert_not_called()

    def test_preflight_time_cannot_bypass_document_deadline(self):
        from PIL import Image
        from sro_robot.core import ocr
        budget = ocr.OcrBudget(allow_native_tesseract=True)
        def expires():
            budget._deadline = 0
        with (
            Image.new("L", (20, 20), 255) as image,
            patch.object(native, "configure_native_tesseract", side_effect=expires),
            patch.object(ocr.pytesseract, "image_to_string") as recognize,
        ):
            with self.assertRaises(ocr.OcrProcessingTimeout):
                ocr._tesseract(image, "--psm 3", budget)
        recognize.assert_not_called()


class NativeRuntimeIntegrationTests(unittest.TestCase):
    """Optional tiny real CLI check; private samples/services are never used."""
    def test_real_text_coordinates_and_rotated_osd_match_system_cli(self):
        command = native.qualified_command()
        if command is None:
            self.skipTest("No qualified Darwin ARM64 runtime")
        from PIL import Image, ImageDraw, ImageFont
        from sro_robot.core import ocr
        font_path = Path("/System/Library/Fonts/Supplemental/Arial.ttf")
        if not font_path.is_file():
            self.skipTest("Local Cyrillic font unavailable")
        font = ImageFont.truetype(str(font_path), 32)
        image = Image.new("L", (2000, 1800), "white")
        try:
            draw = ImageDraw.Draw(image)
            for row in range(30):
                draw.text((60, 30 + row * 50), "Синтетический судебный акт. Проверка текста и направления строк.", font=font, fill=0)
            with image.crop((0, 0, 1500, 200)) as small, image.rotate(90, expand=True) as rotated:
                outputs = []
                for cli in ("tesseract", command):
                    with patch.object(pytesseract.pytesseract, "tesseract_cmd", cli):
                        text_tsv = ocr._tesseract_with_source(small, "--psm 6", None)
                        orientation = pytesseract.image_to_osd(rotated, timeout=10)
                        self.assertTrue(text_tsv[0].strip())
                        self.assertIn("Rotate:", orientation)
                        outputs.append((text_tsv, orientation))
                self.assertEqual(outputs[0], outputs[1])
        finally:
            image.close()


if __name__ == "__main__":
    unittest.main()
