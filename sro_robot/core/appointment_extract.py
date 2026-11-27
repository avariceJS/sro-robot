"""Read source-bound fields for review of bankruptcy hearing appointments."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
import re

from . import morph
from .extract import (
    CASE_NUMBER_TOKEN, MONTHS, MONTHS_RE, NAME_TRIPLE, WORD,
    ROLE_BOUNDARY, _PATRONYMIC_SHAPE, _case_candidate_locations, _court_candidates,
    _operative_postal_address, _practitioner_identifiers, _source_page_at,
    extract_applicant_evidence,
)
from .models import Field
from .person_attributes import detect_person_attributes
from .validators import digits, normalize_case_number

_TITLE = re.compile(
    r"^[ \t]*(?:о[ \t]*п[ \t]*р[ \t]*е[ \t]*д[ \t]*е[ \t]*л[ \t]*е[ \t]*н[ \t]*и[ \t]*е"
    r"|о\s+принятии\s+(?:к\s+производству\s+)?заявления)\b",
    re.I | re.M,
)
_HEADING = re.compile(
    r"^[ \t]*(?:[о0][ \t]*п[ \t]*р[ \t]*е[ \t]*д[ \t]*е[ \t]*л[ \t]*и[ \t]*л[ао]?"
    r"|р[ \t]*е[ \t]*ш[ \t]*и[ \t]*л[ао]?)\s*:",
    re.I | re.M,
)
_QUOTED_HEADING = re.compile(
    r"\b(?:(?:ранее|предыдущ[а-яё]+|цитиру[а-яё]+)\b[^:\f]{0,220}"
    r"\b(?:определени[ея]|решени[ея])|(?:определением|решением)\s+(?:арбитражного\s+)?суда"
    r"[^:\f]{0,150}\b(?:постановлено|решено|определено))\s*:[ \t\r\n«„\"']*\Z",
    re.I,
)
_CASE_LINE = re.compile(rf"\bдело\s*(?:№|N[оo]?|Мº)?\s*(?P<value>{CASE_NUMBER_TOKEN})\b", re.I)
_BODY = re.compile(r"\b(?:рассмотрев|установил[ао]?|поступил[оа]?\s+заявление)\b", re.I)
_BANKRUPTCY = re.compile(r"банкрот\w*|несостоятельн\w*", re.I)
_DATE = re.compile(
    rf"(?<!\d)(?:(?P<d>\d{{1,2}})[./-](?P<m>\d{{2}})[./-](?P<y>\d{{4}})"
    rf"|[«\"]?(?P<day>\d{{1,2}})[»\"]?\s+(?P<month>{MONTHS_RE})\s+(?P<year>\d{{4}}))(?!\d)",
    re.I,
)
_DATE_CAPTION_PREFIX = re.compile(r"\s*(?:дата\s+судебного\s+акта\s*:\s*|г\.\s*[А-ЯЁа-яё -]+\s+)?", re.I)
_FILING_DATE_SUFFIX = re.compile(
    r"^[\s.,:—–-]*(?:дата\s+(?:поступления|получения|регистрации|подачи)|"
    r"(?:входящ[а-яё]+|поступивш[а-яё]+|зарегистрирован[а-яё]*)\b)", re.I,
)
_CLAUSE_START = r"(?:^[ \t]*(?P<clause_number>\d{1,2}[.)][ \t]*)?|(?<=[.;])[ \t]+)"
_HEARING = re.compile(
    rf"{_CLAUSE_START}(?:(?:принять\s+заявление\s+к\s+производству\s*,\s*)?"
    r"назначить\s+(?:(?:предварительное\s+)?судебное\s+заседание"
    r"|рассмотрение\s+обоснованности\s+заявления)\b"
    r"|рассмотрение\s+[«\"‘]?обоснованности\s+заявления"
    r"[^:;\f]{0,300}?\s+назначить\s+(?=на\b))",
    re.I | re.M,
)
_SCHEDULE_SUBJECT = (r"(?:дат[аыуе]|врем(?:я|ени)|мест[оае])\s+(?:проведения\s+)?"
                     r"(?:судебного\s+)?заседания\b")
_HEARING_REVISION = re.compile(
    rf"{_CLAUSE_START}(?:(?:перенести|отложить|отменить)\s+(?:"
    r"(?:(?:назначенное|предварительное|судебное)\s+)*заседание"
    r"|рассмотрение\s+обоснованности\s+заявления)\b"
    rf"|(?:изменить|исправить|уточнить)\b(?:(?<=\d)\.(?=\d)|[^.;:\f]){{0,200}}\b{_SCHEDULE_SUBJECT}"
    rf"|{_SCHEDULE_SUBJECT}\s+(?:изменить|исправить|уточнить)\b"
    rf"|внести\s+(?:исправлени[ея]|изменени[ея]|уточнени[ея])\s+во?\s+{_SCHEDULE_SUBJECT})",
    re.I | re.M,
)
_HEARING_STOP = re.compile(
    r";|\f|\b(?:в\s+(?:помещении|здании|зале)|по\s+адресу|заседание\s+состоится)\b"
    r"|[.!?]\s+(?=(?:заседание|лицам|копию|судебный\s+акт)\b)"
    r"|(?<!\bг)(?<!\bчас)(?<!\bмин)\.(?=\s+(?-i:[А-ЯЁ]))"
    r"|[,]\s*(?:обязав|предложив|поручив|установив|представить|направить)\b"
    r"|\n[ \t]*(?:\d{1,2}[.)][ \t]+|"
    r"(?:обязать|направить|предложить|истребовать|судья)\b)",
    re.I,
)
_SCHEDULE_CONNECTOR = re.compile(r"\s*(?:(?:г\.?|года|мск)\s*)?,?\s*(?:(?:на|в)\s+)?", re.I)
_TIME = re.compile(
    r"(?<![\d.])(?P<hour>\d{1,2})(?:[ \t]*:[ \t]*(?P<minute>\d{2})"
    r"|[\s'’]*(?:час(?:а|ов)?[.,]?|ч\.)\s*(?P<minutes>\d{1,2})[\s,|)]*"
    r"(?:мин(?:ут(?:а|ы)?)?\b\.?|м\.))"
    r"(?!\d)",
    re.I,
)
_NAME = rf"{WORD}(?:[ \t]*\({WORD}\))?\s+{WORD}\s+{WORD}"
_PARTY_BIO = r"(?:\s*\([^\f()]{1,450}\))?"
_SELF_END = r"о\s+признании\s+себя\s+(?:несостоятельн(?:ым|ой)\s*\(\s*)?банкротом\b"
_APPLICATION = re.compile(
    rf"(?im:(?:\b(?:рассмотрев\s+|принять\s+|поступил[оа]\s+)|^[ \t]*\d{{1,2}}[.)][ \t]*)"
    r"заявлени[ея]\s+(?:граждан(?:ина|ки)\s+)?)"
    rf"(?P<name>{_NAME}){_PARTY_BIO}(?i:\s*,?\s*(?P<self>{_SELF_END})"
    r"|\s*,?\s*(?=о\s+признании\b|к\s+производству\b))",
)
_SELF_APPLICANT = re.compile(
    rf"(?P<name>{_NAME})\s*\([^\f()]{{0,65}}\b(?i:должник)\s*\)"
    rf"(?i:\s+обратил(?:ся|ась)\s+в\s+арбитражный\s+суд[^;.\f]{{0,100}}?\bс\s+заявлением\s+{_SELF_END})",
)
_PARTY_LABEL = re.compile(
    r"(?im)^[ \t]*(?P<label>заявитель|должник)[ \t]*[:—-][ \t]*(?P<value>[^\r\n\f;]{0,160})"
)
_DEBTOR_BIOGRAPHY = re.compile(
    rf"(?im:^[ \t]*должник\b[ \t]*(?:[:—-][ \t\r\n]*|[ \t\r\n]+)(?:граждан(?:ин|ка)[ \t]+)?)"
    rf"(?P<name>{_NAME})[ \t\r\n]*\((?P<bio>[^\f()]{{1,500}})\)",
)
_BIOGRAPHY_LABELS = {
    'inn': re.compile(r"\bинн[ \t\r\n]*[:№]?[ \t\r\n]*$", re.I),
    'snils': re.compile(r"\bснилс[ \t\r\n]*[:№]?[ \t\r\n]*$", re.I),
    'birth_date': re.compile(r"\bдата\s+рождения\s*[:—-]?\s*$", re.I),
}
_REGISTRATION_LABEL = re.compile(r"\bадрес\s+регистрации\s*:\s*$", re.I)
_JUDGE_NAME = rf"(?:{WORD}[ \t]+[А-ЯЁ]\.[ \t]*[А-ЯЁ]\.|[А-ЯЁ]\.[ \t]*[А-ЯЁ]\.[ \t]+{WORD})"
_JUDGE = re.compile(
    rf"(?i:\b(?:в[ \t]+составе[ \t]+)?судь[яие][ \t]*[:—-]?[ \t]+"
    rf"(?:арбитражного[ \t]+суда[ \t]+(?:[а-яё-]+[ \t]+){{1,6}}?)?)(?P<name>{_JUDGE_NAME})"
)
_PRACTITIONER_ROLE = r"(?:(?:финансовым|конкурсным|временным|внешним|арбитражным)\s+)?управляющим"
_PRACTITIONER = re.compile(
    rf"(?im:{_CLAUSE_START}утвердить\s+)(?:"
    rf"(?i:{_PRACTITIONER_ROLE}\s+)(?P<after_role>{_NAME})(?=[ \t]*(?:[,.(;]|\r?\n|\Z))"
    rf"|(?P<before_role>{_NAME})(?i:\s+{_PRACTITIONER_ROLE}\b))",
)


def _date_value(match: re.Match) -> str | None:
    try:
        if match['y']:
            value = date(int(match['y']), int(match['m']), int(match['d']))
        else:
            value = date(int(match['year']), MONTHS[match['month'].casefold()], int(match['day']))
    except ValueError:
        return None
    return value.strftime('%d.%m.%Y')


def _time_value(match: re.Match) -> str | None:
    hour, minute = int(match['hour']), int(match['minute'] or match['minutes'])
    return f'{hour:02d}:{minute:02d}' if hour < 24 and minute < 60 else None


def _name_value(raw: str) -> str:
    name = NAME_TRIPLE.fullmatch(raw)
    normalized = morph.person_triplet(name.groups()) if name else None
    return ' '.join(normalized or raw.split())


def _debtor_biography_fields(text: str, start: int, end: int):
    bio = text[start:end]
    if ROLE_BOUNDARY.search(bio) or any(_PATRONYMIC_SHAPE.fullmatch(match[3]) for match in NAME_TRIPLE.finditer(bio)):
        return
    fields = detect_person_attributes(text, start, end)
    for field, label in _BIOGRAPHY_LABELS.items():
        span = getattr(fields, field + '_location')
        if span and label.search(text[start:span[0]]):
            yield 'debtor_' + field, getattr(fields, field), span
    postal = _operative_postal_address(bio)
    if postal:
        value, kind, (left, right), partial = postal
        if kind == 'registration' and not partial:
            yield 'debtor_registration_address', value, (start + left, start + right)
    elif (fields.address_kind == 'registration' and not fields.address_partial and fields.address_location
          and _REGISTRATION_LABEL.search(text[start:fields.address_location[0]])):
        yield 'debtor_registration_address', fields.address, fields.address_location


def _readings(variants):
    if not isinstance(variants, list) or not 1 <= len(variants) <= 5:
        return []
    return variants if all(isinstance(text, str) and len(text) <= 200_000 for text in variants) else []


def _operative(text):
    headings = list(_HEADING.finditer(text))
    if len(headings) != 1:
        return None
    (heading,) = headings
    prefix = text[max(0, heading.start() - 270):heading.start()]
    return None if _QUOTED_HEADING.search(prefix) else heading


def _operative_instructions(pattern, text, start):
    for match in pattern.finditer(text, start):
        previous = match.start() - 1
        while previous >= start and text[previous].isspace():
            previous -= 1
        numbered = match['clause_number'] and (previous < start or text[previous] not in ':«„"\'')
        if numbered or previous < start or text[previous] in '.;':
            yield match


def has_hearing_appointment(variants: list[str]) -> bool:
    """Check whether an operative clause identifies a hearing even if its schedule is unreadable."""
    return any(
        _BANKRUPTCY.search(text) and (heading := _operative(text))
        and any(next(_operative_instructions(pattern, text, heading.end()), None)
                for pattern in (_HEARING, _HEARING_REVISION))
        for text in _readings(variants)
    )


def extract_appointment_fields(variants: list[str], caption_readings: list[str] | None = None) -> dict[str, dict]:
    """Return names, caption and current hearing with offsets in original readings."""
    variants = _readings(variants)
    if not variants:
        return {}
    captions = _readings(caption_readings)[:2]
    proposals = defaultdict(list)
    paired_readings = set()
    revised_hearing = False
    primary_cases = set()
    for index, text in enumerate([*variants, *captions]):
        if index == len(variants):
            primary_cases = {item['value'] for item in proposals.get('case_number', ())}
        if not _BANKRUPTCY.search(text):
            continue
        heading = _operative(text)
        initial_end = min(heading.start() if heading else len(text), 5000)
        title = _TITLE.search(text, 0, initial_end)
        body = _BODY.search(text, title.end() if title else 0, initial_end)
        header_end = min(body.start() if body else initial_end, (title.end() + 440) if title else 1900)
        header = text[:header_end]

        def add(key, value, start, end):
            if value:
                proposals[key].append({
                    'value': value, 'source': 'appointment:text', 'source_variant': index,
                    'source_start': start, 'source_end': end, 'source_page': _source_page_at(text, start),
                })

        cases = [(normalize_case_number(m['value']), *m.span('value')) for m in _CASE_LINE.finditer(header)]
        if not cases and title:
            cases = list(_case_candidate_locations(header[title.end():]))
            cases = [(v, start + title.end(), end + title.end()) for v, start, end in cases]
        if index >= len(variants) and primary_cases:
            identified = {value for value, *_ in cases} or {
                normalize_case_number(match['value']) for match in _CASE_LINE.finditer(text)
            }
            if identified - primary_cases:
                continue
        if index < len(variants) or not primary_cases:
            for value, start, end in cases:
                add('case_number', value, start, end)
        for value, start, end in _court_candidates(header):
            if re.fullmatch(r"Арбитражный суд(?: [А-ЯЁа-яё-]+){1,6}", value, re.I) and not re.search(
                r"\b(?:суд\s+суд|заявление|официальн\w*\s+сайт)\b", value, re.I
            ):
                add('court', value, start, end)
        for match in _JUDGE.finditer(text, 0, min(initial_end, header_end + 280)):
            add('judge_name', ' '.join(match['name'].split()), *match.span('name'))
        if title:
            for match in _DATE.finditer(header, title.end()):
                line_start = max(title.end(), text.rfind('\n', title.end(), match.start()) + 1)
                prefix = header[line_start:match.start()]
                suffix = header[match.end():].split('\n', 1)[0]
                if _DATE_CAPTION_PREFIX.fullmatch(prefix) and not _FILING_DATE_SUFFIX.search(suffix):
                    add('doc_date', _date_value(match), *match.span())
        matches = list(_APPLICATION.finditer(text, 0, min(len(text), initial_end + 800)))
        matches.extend(_SELF_APPLICANT.finditer(text, 0, initial_end))
        for match in matches:
            name = _name_value(match['name'])
            add('applicant', name, *match.span('name'))
            if match.re is _SELF_APPLICANT or match['self']:
                add('debtor_name', name, *match.span('name'))
        for match in _PARTY_LABEL.finditer(text, 0, initial_end):
            raw = match['value'].strip(' \t,.')
            field = 'applicant' if match['label'].casefold() == 'заявитель' else 'debtor_name'
            if not raw:
                continue
            start = text.index(raw, *match.span('value'))
            literal_name = NAME_TRIPLE.match(raw) if field == 'debtor_name' and '(' in raw else None
            if literal_name:
                raw = literal_name[0]
            add(field, _name_value(raw), start, start + len(raw))
        for match in _DEBTOR_BIOGRAPHY.finditer(text, 0, initial_end):
            if '\f' in match[0]:
                continue
            add('debtor_name', _name_value(match['name']), *match.span('name'))
            for key, value, offsets in _debtor_biography_fields(text, *match.span('bio')):
                add(key, value, *offsets)
        if not heading or index >= len(variants):
            continue
        hearings = list(_operative_instructions(_HEARING, text, heading.end()))
        # A later correction/cancellation cannot leave the original schedule current.
        # Keep the hearing event visible for review rather than guessing a replacement.
        revised = next(_operative_instructions(_HEARING_REVISION, text, heading.end()), None)
        revised_hearing = revised_hearing or revised is not None
        if len(hearings) == 1 and not revised:
            start = hearings[0].end()
            limit = min(len(text), start + 420)
            boundary = _HEARING_STOP.search(text, start, limit)
            limit = boundary.start() if boundary else limit
            dates = [(value, m) for m in _DATE.finditer(text, start, limit) if (value := _date_value(m))]
            times = [(value, m) for m in _TIME.finditer(text, start, limit) if (value := _time_value(m))]
            if len(dates) == 1:
                add('hearing_date', dates[0][0], *dates[0][1].span())
            if len(times) == 1:
                time_value, time_match = times[0]
                if not dates:
                    add('hearing_time', time_value, *time_match.span())
                elif len(dates) == 1:
                    date_value, date_match = dates[0]
                    earlier, later = sorted((date_match, time_match), key=lambda match: match.start())
                    if _SCHEDULE_CONNECTOR.fullmatch(text[earlier.end():later.start()]):
                        add('hearing_time', time_value, *time_match.span())
                        paired_readings.add((date_value, time_value))
        for match in _operative_instructions(_PRACTITIONER, text, heading.end()):
            name = 'after_role' if match['after_role'] else 'before_role'
            add('practitioner', _name_value(match[name]), *match.span(name))

    result = {}
    for key, rows in proposals.items():
        primary = [row for row in rows if row['source_variant'] < len(variants)]
        choices = rows if key.startswith('debtor_') and key != 'debtor_name' else primary or rows
        if len({row['value'].casefold().replace('ё', 'е') for row in choices}) == 1:
            result[key] = choices[0]
    all_debtor_names = {row['value'].casefold().replace('ё', 'е') for row in proposals.get('debtor_name', ())}
    if len(all_debtor_names) > 1 or 'debtor_name' not in result:
        for key in tuple(result):
            if key.startswith('debtor_') and key != 'debtor_name':
                del result[key]
    if 'debtor_inn' in result or 'debtor_snils' in result:
        for field, numbers in _practitioner_identifiers(variants).items():
            key = 'debtor_' + field
            if key in result and digits(result[key]['value']) in numbers:
                del result[key]
    if 'applicant' not in proposals and 'case_number' in result:
        evidence = extract_applicant_evidence(variants, Field(value=result['case_number']['value']))
        if evidence:
            result['applicant'] = {
                'value': evidence['name'], 'source': 'appointment:text',
                **{key: evidence[key] for key in ('source_variant', 'source_start', 'source_end', 'source_page')},
            }
    if 'hearing_date' in result and 'hearing_time' in result:
        pair = result['hearing_date']['value'], result['hearing_time']['value']
        if pair not in paired_readings:
            del result['hearing_time']
    if revised_hearing:
        result.pop('hearing_date', None)
        result.pop('hearing_time', None)
    if 'doc_date' in result and 'hearing_date' in result:
        act = tuple(map(int, reversed(result['doc_date']['value'].split('.'))))
        hearing = tuple(map(int, reversed(result['hearing_date']['value'].split('.'))))
        if hearing < act:
            if result['doc_date']['source_variant'] >= len(variants):
                result.pop('doc_date')
            else:
                result.pop('hearing_date')
                result.pop('hearing_time', None)
    return result
