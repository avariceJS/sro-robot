"""Synthetic equivalence and work-avoidance checks for reverse debtor scans."""

from dataclasses import asdict
import re
import unittest
from unittest.mock import patch

from hypothesis import given, settings, strategies as st

from sro_robot.core import extract


# Freeze the current admitted lexical contract independently of extract.WORD.
# This oracle always scans: only the necessary-suffix optimization is compared.
# Dangling/repeated dashes and suffixes within damaged tokens are not names;
# orthographic Unicode hyphens retain their literal spelling and source spans.
_ADMITTED_NAME_WORD = (
    r"(?<![\w‐‑‒–—−-])[А-ЯЁ][а-яёА-ЯЁ]+"
    r"(?:[-‐‑][а-яёА-ЯЁ]+)*(?![\w‐‑‒–—−-])"
)
_UNGUARDED_REVERSE_NAME = re.compile(
    rf"(?P<name>{_ADMITTED_NAME_WORD}\s+{_ADMITTED_NAME_WORD}\s+"
    rf"{_ADMITTED_NAME_WORD})\s*"
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
            _match_evidence(_UNGUARDED_REVERSE_NAME.finditer(text)),
        )
        for match in matches:
            self.assertIs(match.re, extract.REVERSE_PERSON_DEBTOR_NAME)
            self.assertIs(match.string, text)

    def test_spacing_case_hyphens_and_suffix_near_misses(self):
        for name in (
            "Синтетиков Тест Тестович", "СИНТЕТИКОВ ТЕСТ ТЕСТОВИЧ",
            "синтетиков тест тестович", "Ёлкин-Тест Тест Ёжович",
            "Ёлкин‐Тест Тест Ёжович", "Ёлкин‑Тест Тест Ёжович",
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
            "Ёлкин‐Тест Тест Ёжович", "Ёлкин‑Тест Тест Ёжович",
        )),
        st.text(alphabet="АБВЁабвё-‐‑‒–—−X7_должникДАЛЕЕ() \t\n\r\u00a0", max_size=70),
    ), max_size=15))
    def test_generated_match_groups_and_all_source_spans(self, fragments):
        self.assertEquivalent("".join(fragments))

    def test_malformed_tokens_from_old_grammar_are_explicitly_rejected(self):
        for name in (
            "Ё-\u00a0Ё-\u00a0Ё-", "Ёлкин- Тест Ёжович", "Ёлкин--Тест Тест Ёжович",
            "Ёлкин–Тест Тест Ёжович", "XЁлкин Тест Ёжович", "7Ёлкин Тест Ёжович",
            "_Ёлкин Тест Ёжович", "Ёлкин Тест ЁжовичX",
        ):
            with self.subTest(name=name):
                text = name + "(должник)"
                self.assertEqual(list(extract._reverse_person_debtor_matches(text)), [])
                self.assertEqual(list(_UNGUARDED_REVERSE_NAME.finditer(text)), [])

    def test_supported_compound_spans_are_known_independently_of_the_oracle(self):
        for hyphen in ('-', '‐', '‑'):
            with self.subTest(hyphen=hyphen):
                prefix = "Вводный текст.\f"
                name = f"Ёлкин{hyphen}Тест Тест Ёжович"
                text = prefix + name + "\n(должник)"
                matches = list(extract._reverse_person_debtor_matches(text))
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0].group('name'), name)
                self.assertEqual(matches[0].span(), (len(prefix), len(text)))
                self.assertEqual(matches[0].span('name'), (len(prefix), len(prefix) + len(name)))

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
                    side_effect=_UNGUARDED_REVERSE_NAME.finditer,
                ):
                    unguarded = asdict(extract.parse_variants_for_review(variants))
                self.assertEqual(actual, unguarded)
