"""Synthetic image checks; no private sources, OCR, database, or app."""
import unittest
from PIL import Image, ImageDraw
from scripts.dev.quality import ocr_illumination as illumination
from scripts.dev.quality.ocr_research import distort


class IlluminationGateTests(unittest.TestCase):
    def test_uniform_white_dark_and_low_contrast_pages_keep_baseline(self):
        for value in (255, 180, 90):
            image = Image.new("RGB", (600, 800), (value, value, value))
            self.assertEqual(illumination.choose_recognizer(image), "baseline")
        image = Image.new("RGB", (600, 800), "white")
        ImageDraw.Draw(image).rectangle((40, 40, 150, 50), fill="black")
        for variant in ("clean", "blur", "tilt", "perspective", "lighting"):
            self.assertEqual(illumination.choose_recognizer(distort(image, variant)), "baseline")

    def test_controlled_shadow_selects_adaptive_without_any_readings(self):
        image = Image.new("RGB", (600, 800), "white")
        ImageDraw.Draw(image).rectangle((40, 40, 150, 50), fill="black")
        self.assertEqual(illumination.choose_recognizer(distort(image, "shadow")), "sauvola")
        self.assertGreater(illumination.background_range(distort(image, "shadow")), illumination.THRESHOLD)

    def test_replay_requires_exact_producer_and_bounded_dpi_before_render(self):
        from scripts.dev.quality.ocr_research import VARIANTS
        producer = {"synthetic": "producer"}
        config = {"schema": 1, "dpi": 200, "variants": list(VARIANTS),
                  "recognizers": ["baseline", "sauvola"], "fingerprint": producer}
        illumination.validate_cohort(config, producer)
        for invalid in (True, 9999):
            config["dpi"] = invalid
            with self.assertRaises(ValueError):
                illumination.validate_cohort(config, producer)
        config["dpi"] = 200
        with self.assertRaises(ValueError):
            illumination.validate_cohort(config, {"different": "producer"})

    def test_plan_rejects_exposed_case_overlap_or_changed_algorithm(self):
        plan = {"schema": 1, "algorithm": illumination.ALGORITHM, "threshold": .20,
                "fit_case_ids": ["synthetic-fit"], "excluded_exposed_case_ids": ["synthetic-exposed"],
                "control_inputs": [{"sha256": "a" * 64, "case_id": "synthetic-control"}]}
        self.assertEqual(illumination.validate_plan(plan), {"a" * 64})
        plan["control_inputs"][0]["case_id"] = "synthetic-fit"
        with self.assertRaises(ValueError):
            illumination.validate_plan(plan)
        plan["control_inputs"][0]["case_id"] = "synthetic-control"
        plan["threshold"] = .19
        with self.assertRaises(ValueError):
            illumination.validate_plan(plan)


if __name__ == "__main__":
    unittest.main()
