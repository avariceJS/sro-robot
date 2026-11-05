from __future__ import annotations

import unittest
from contextlib import nullcontext
from unittest.mock import patch
from PIL import Image
from sro_robot.core import ocr
from sro_robot.core.extract import parse_file

from sro_robot.core.source_locations import SourceLocationCollector, words_from_tsv


TSV = """level\tpage_num\tleft\ttop\twidth\theight\tconf\ttext
1\t1\t0\t0\t100\t30\t-1\t
5\t1\t20\t10\t30\t10\t96\tДело
5\t1\t55\t10\t25\t10\t90\t123
"""


class SourceLocationTests(unittest.TestCase):
    def test_pdf_layout_keeps_leading_space_page_offsets_and_physical_line_order(self):
        collector = SourceLocationCollector()
        text = "  Header\nLeft Right\n\f\nSecond"
        xml = '''<html><body><page width="100" height="100">
          <word xMin="5" yMin="5" xMax="40" yMax="10">Header</word>
          <word xMin="55" yMin="20" xMax="85" yMax="25">Right</word>
          <word xMin="5" yMin="20" xMax="30" yMax="25">Left</word>
          </page><page width="100" height="100">
          <word xMin="5" yMin="5" xMax="40" yMax="10">Second</word>
          </page></body></html>'''
        collector.record_pdf_text(0, text, xml)
        normalized = text.replace("  ", " ")
        for value, page in (("Header", 1), ("Left", 1), ("Right", 1), ("Second", 2)):
            start = normalized.index(value)
            boxes = collector.boxes_for_span(0, start, start + len(value))
            self.assertEqual(len(boxes), 1)
            self.assertEqual(boxes[0]["page"], page)

    def test_maps_tsv_words_to_parser_normalized_text_and_unit_boxes(self):
        words = words_from_tsv(TSV, "\tДело\u00a0 123\n", page_size=(100, 50))
        self.assertEqual([(word.start, word.end) for word in words], [(1, 5), (6, 9)])
        self.assertEqual(words[0].as_dict(), {
            "page": 1, "x": 0.2, "y": 0.2, "width": 0.3, "height": 0.2,
        })

    def test_binds_only_words_overlapping_exact_source_span(self):
        collector = SourceLocationCollector()
        collector.record_variant(2, [("Дело 123", TSV, (100, 50))])
        self.assertEqual(collector.boxes_for_span(2, 5, 8), [{
            "page": 1, "x": 0.55, "y": 0.2, "width": 0.25, "height": 0.2,
        }])
        self.assertEqual(collector.boxes_for_span(1, 0, 9), [])

    def test_rotated_ocr_boxes_map_back_to_decoded_photo(self):
        tsv = "level\tleft\ttop\twidth\theight\tconf\ttext\n5\t70\t10\t20\t20\t96\tДело\n"
        for rotation, expected in (
            (90, (0.1, 0.1, 0.2, 0.2)),
            (180, (0.1, 0.7, 0.2, 0.2)),
            (270, (0.7, 0.7, 0.2, 0.2)),
        ):
            collector = SourceLocationCollector()
            collector.record_variant(0, [("Дело", tsv, (100, 100))], rotations=[rotation])
            box = collector.boxes_for_span(0, 0, 4)[0]
            for key, value in zip(("x", "y", "width", "height"), expected):
                self.assertAlmostEqual(box[key], value)

    def test_parse_file_attaches_geometry_from_same_recognition_result(self):
        text = "Дело № А99-123/2099"
        tsv = "level\tleft\ttop\twidth\theight\tconf\ttext\n5\t20\t10\t50\t10\t96\tА99-123/2099\n"
        def recognize(_path, **options):
            options["source_location_collector"].record_variant(0, [(text, tsv, (100, 50))])
            return [text]
        with patch("sro_robot.core.ocr.recognize", side_effect=recognize):
            card = parse_file("synthetic.png")
        self.assertEqual(card.case_number.value, "А99-123/2099")
        self.assertEqual(card.case_number.source_boxes, [{
            "page": 1, "x": 0.2, "y": 0.2, "width": 0.5, "height": 0.2,
        }])

    def test_missing_tsv_keeps_text_without_second_tesseract_process(self):
        with (
            patch("sro_robot.core.ocr.pytesseract.pytesseract.save", return_value=nullcontext(("/synthetic", "/synthetic.png"))),
            patch("sro_robot.core.ocr.pytesseract.pytesseract.run_tesseract") as run,
            patch("sro_robot.core.ocr.pytesseract.pytesseract._read_output", side_effect=["Дело 123", OSError("missing tsv")]),
        ):
            self.assertEqual(ocr._tesseract_with_source(Image.new("L", (1, 1)), "--psm 3", None), ("Дело 123", ""))
        run.assert_called_once()
        self.assertIn("txt tsv", run.call_args.args)
        self.assertIn("--psm 3", run.call_args.kwargs["config"])

    def test_name_component_boxes_do_not_include_other_names_or_old_surname(self):
        text = "Ивановой (Петровой) Анны Сергеевны"
        tsv = "level\tleft\ttop\twidth\theight\tconf\ttext\n" + "".join(
            f"5\t{index * 20}\t10\t18\t10\t96\t{word}\n"
            for index, word in enumerate(text.split())
        )
        collector = SourceLocationCollector()
        collector.record_variant(0, [(text, tsv, (100, 50))])
        full = collector.boxes_for_span(0, 0, len(text))
        self.assertEqual(len(full), 4)
        for attr, word_index in (("last_name", 0), ("first_name", 2), ("middle_name", 3)):
            self.assertEqual(collector.boxes_for_field(attr, 0, 0, len(text)), [full[word_index]])
        self.assertEqual(collector.boxes_for_field("fio_raw", 0, 0, len(text)), full)
        self.assertEqual(collector.boxes_for_field("last_name", 0, 0, len("Ивановой")), [full[0]])
        self.assertEqual(collector.boxes_for_field("last_name", 0, 0, len("Ивановой (Петровой)")), [])


    def test_invalid_or_unmatched_tsv_does_not_invent_locations(self):
        self.assertEqual(words_from_tsv(TSV, "unrelated text", page_size=(100, 50)), [])
        self.assertEqual(words_from_tsv(TSV, "Дело 123", page_size=(0, 50)), [])


if __name__ == "__main__":
    unittest.main()
