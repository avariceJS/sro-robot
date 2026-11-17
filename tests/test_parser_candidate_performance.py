"""Synthetic equivalence and work-avoidance checks for reverse debtor scans."""

from dataclasses import asdict
import re
import unittest
from unittest.mock import patch

from hypothesis import given, settings, strategies as st

from sro_robot.core import extract


# Independent copy of the pre-guard expression: changes to the production
# expression must not silently change the equivalence oracle.
_ORIGINAL_REVERSE_NAME = re.compile(
    r"(?P<name>[А-ЯЁ][а-яёА-ЯЁ\-]+\s+[А-ЯЁ][а-яёА-ЯЁ\-]+\s+"
    r"[А-ЯЁ][а-яёА-ЯЁ\-]+)\s*"
    r"\(\s*(?:далее\s*[-—]\s*)?должник\s*\)",
    re.I,
)
_LEGAL_PROSE = (
    "Арбитражный суд рассмотрел заявление и установил следующие обстоятельства. "
    "Материалы дела содержат сведения об исполнении обязательств сторонами. "
    "Судебное заседание назначено после исследования представленных доказательств.\n"
)
_SUFFIXES = (
    "(должник)", " (ДОЛЖНИК)", "\n( далее-должник )", "(далее — должник)",
    "(\tДаЛеЕ\n—\tДоЛжНиК\r\n)", "\u00a0(\u2003далее\u2028-\u2009должник\u3000)",
    "(далее должник)", "(далее–должник)", "(далее--должник)", "(должника)",
    "(недолжник)", "(должник", "должник)", "(далее-должник extra)",
    "(далее-должниКи)", "(далее-должник))", "((должник)", "[должник]", "",
)


def _match_evidence(matches):
    return [
        (match.group(), match.groups(), match.groupdict(), match.regs,
         match.pos, match.endpos)
        for match in matches
    ]


class ReverseDebtorScanTests(unittest.TestCase):
    def assertEquivalent(self, text):
        matches = list(extract._reverse_person_debtor_matches(text))
        self.assertEqual(
            _match_evidence(matches),
            _match_evidence(_ORIGINAL_REVERSE_NAME.finditer(text)),
        )
        for match in matches:
            self.assertIs(match.re, extract.REVERSE_PERSON_DEBTOR_NAME)
            self.assertIs(match.string, text)

    def test_spacing_case_hyphens_and_suffix_near_misses(self):
        for name in (
            "Синтетиков Тест Тестович", "СИНТЕТИКОВ ТЕСТ ТЕСТОВИЧ",
            "синтетиков тест тестович", "Ёлкин-Тест Тест Ёжович",
        ):
            for separator in (" ", "\n", "\t", "\r\n", "\u00a0", "\u2028"):
                for suffix in _SUFFIXES:
                    with self.subTest(name=name, separator=separator, suffix=suffix):
                        occurrence = name.replace(" ", separator) + suffix
                        self.assertEquivalent("Вводный текст.\f" + occurrence + "; " + occurrence)

    @settings(max_examples=400, derandomize=True, deadline=None)
    @given(fragments=st.lists(st.one_of(
        st.sampled_from(_SUFFIXES + (
            "Синтетиков Тест Тестович", "синтетиков\nтест\tтестович",
            "Ёлкин-Тест Тест Ёжович", _LEGAL_PROSE, "\f", "; ",
        )),
        st.text(alphabet="АБВЁабвё-должникДАЛЕЕ() \t\n\r\u00a0", max_size=70),
    ), max_size=15))
    def test_generated_match_groups_and_all_source_spans(self, fragments):
        self.assertEquivalent("".join(fragments))

    def test_full_scan_is_skipped_by_both_callers_without_required_suffix(self):
        text = _LEGAL_PROSE * 200 + "Синтетиков Тест Тестович (далее должник)"
        self.assertEquivalent(text)
        with patch.object(extract, "REVERSE_PERSON_DEBTOR_NAME") as full_scan:
            extract._role_evidence(text)
            extract._application_candidates(text)
        full_scan.finditer.assert_not_called()

    def test_matching_has_no_name_length_or_document_position_cutoff(self):
        long_name = " ".join(("Синтетиков" * 80, "Тест" * 80, "Тестович" * 80))
        text = _LEGAL_PROSE * 200 + long_name + "\n(далее-должник)"
        self.assertEquivalent(text)
        self.assertEqual(len(list(extract._reverse_person_debtor_matches(text))), 1)

    def test_full_parse_payload_preserves_variants_and_occurrence_provenance(self):
        cases = (
            [_LEGAL_PROSE * 20],
            ["Синтетиков Тест Тестович (далее — должник)."],
            ["Синтетиков\nТест Тестович (должник)",
             "СИНТЕТИКОВ ТЕСТ ТЕСТОВИЧ (ДОЛЖНИК)"],
            ["Представитель Синтетиков Тест Тестович (должник).\n"
             "Ёлкин-Тест Тест Ёжович (далее-должник)."],
            ["Синтетиков Тест Тестович (должник).\f"
             "Другов Тест Тестович (должник)."],
        )
        for variants in cases:
            with self.subTest(variants=variants):
                actual = asdict(extract.parse_variants_for_review(variants))
                with patch.object(
                    extract, "_reverse_person_debtor_matches",
                    side_effect=_ORIGINAL_REVERSE_NAME.finditer,
                ):
                    original = asdict(extract.parse_variants_for_review(variants))
                self.assertEqual(actual, original)
