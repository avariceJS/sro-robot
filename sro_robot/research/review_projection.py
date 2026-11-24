"""Owner-review projection of existing results, never authoritative extraction.

The maintained office occurrence projector supplies person candidates. This
adapter adds the research card schema and refuses conflicting identity bundles.
No OCR, application settings, database engine or external record is used.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import asdict
from datetime import date
import hashlib
import json
import math
import re
from types import SimpleNamespace

from app.services.fields import review_fields_with_occurrence
from sro_robot.core.extract import (
    BANKRUPTCY_PERSON_NAME, CASE_NUMBER_TOKEN, INSOLVENCY_CASE_NAME, NAME_TRIPLE, _DECISION_TITLE,
    _OPERATIVE_NAME, _OTHER_ACT_TITLE, _date_candidates, _role_identity_key,
)
from sro_robot.core.validators import digits, normalize_case_number, valid_inn, valid_ogrn, valid_ogrnip, valid_snils
from .review_schema import FIELDS, NAME_PARTS

PROJECTION_VERSION = 'owner-review-card:v3'
KINDS = {'person', 'legal_entity', 'sole_proprietor'}
COMMON = ('entity_kind', 'case_number', 'doc_date', 'court', 'doc_type')
PERSON = NAME_PARTS + ('inn', 'snils', 'address', 'address_kind')
LEGAL = ('legal_name', 'short_name', 'legal_form', 'inn', 'ogrn', 'kpp', 'address', 'address_kind')
IP = ('debtor_name', 'inn', 'ogrnip', 'address', 'address_kind')
PARTY = set(FIELDS) - set(COMMON)
WARNING = 'Кандидат OCR для проверки; роль и принадлежность выбранному участнику подтвердите по оригиналу.'
ROLES = {'debtor', 'defendant', 'plaintiff', 'applicant', 'interested_party'}
PARTICIPANTS = {'entity_participant', 'caption_participant'}
EXPERT_VERSION = 'expert-verdict:v1'
EXPERT_FIELDS = {
    'person': ('fio_raw', 'inn', 'snils', 'address', 'case_number', 'doc_date'),
    'legal_entity': ('legal_name', 'inn', 'ogrn', 'address', 'case_number', 'doc_date'),
    'sole_proprietor': ('debtor_name', 'inn', 'ogrnip', 'address', 'case_number', 'doc_date'),
}
_BANKRUPTCY_TERMS = re.compile(r"банкрот|несостоятельност|конкурсн\w+\s+производств", re.I)
_EXPERT_CASE_LINE = re.compile(rf"\bдело[ \t]*[№N]?[ \t]*(?P<number>{CASE_NUMBER_TOKEN})[ \t]*\r?\n", re.I)
_EXPERT_HEARING = re.compile(r"\bрассмотрев\b", re.I)
_EXPERT_FINDINGS = re.compile(r"(?im)^[ \t]*у[ \t]*с[ \t]*т[ \t]*а[ \t]*н[ \t]*о[ \t]*в[ \t]*и[ \t]*л[ао]?[ \t]*:")
_BANKRUPTCY_CAPTION = re.compile(
    r"\b(?:дел[оеу]\s+о\s+(?:несостоятельности|банкротстве)|"
    r"о\s+признании\s+[^.;:!\f]{1,180}несостоятельн(?:ым|ой)\s*\(\s*банкротом\s*\))", re.I,
)
_CIVIL_CAPTION = re.compile(r"\bиск\w*\s+о\s+взыскании\b|\bисков\w*\s+заявлени\w*", re.I)
_REFERENCED_DISPOSITION = re.compile(r"\b(?:цитир\w*|выдержк\w*|воспроизвод\w*|выписк\w*)\b", re.I)
_CAPTION_SUBJECT = re.compile(
    r"\b(?:дел[оеу]\s+о\s+(?:банкротстве|несостоятельности)(?:\s*\(\s*банкротстве\s*\))?|"
    r"о\s+признании)\s+", re.I,
)
_CAPTION_PERSON = re.compile(
    rf"\bо\s+признании\s+(?:гражданина\s+|гражданки\s+)?(?P<name>{_OPERATIVE_NAME})\s+"
    r"несостоятельн(?:ым|ой)\s*\(\s*банкротом\s*\)", re.I,
)
_CAPTION_SELF_APPLICATION = re.compile(
    rf"\bзаявлени[ея]\s+(?:гражданина\s+|гражданки\s+)?(?P<name>{_OPERATIVE_NAME})\s*"
    r"(?:\([^()\f]{1,160}\)\s*)?о\s+признании\s+(?:его|е[её]|себя)\s+"
    r"несостоятельн(?:ым|ой)\s*\(\s*банкротом\s*\)", re.I,
)
_EXPLICIT_DEBTOR_LINE = re.compile(r"(?im)^[ \t]*должник[ \t]*[:—–][ \t]*")
_EXPERT_ROLE_SOURCES = {
    'person': 'debtor_role:v1',
    'legal_entity': 'debtor_entity_role:v1',
    'sole_proprietor': 'debtor_entity_role:v1',
}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def value(field):
    result = field.get('value') if isinstance(field, dict) else field
    return result if isinstance(result, str) and result.strip() else None


def normalized(text):
    return ' '.join((text or '').split()).casefold()


def applicable_fields(kind):
    extra = {'person': PERSON, 'legal_entity': LEGAL, 'sole_proprietor': IP}.get(kind)
    return list(COMMON + extra) if extra is not None else list(COMMON)


def uncertain(field, kind):
    item = deepcopy(field)
    try:
        confidence = float(item.get('confidence') or 0)
    except (ValueError, TypeError, OverflowError):
        confidence = 0.
    confidence = confidence if math.isfinite(confidence) and 0 <= confidence <= 1 else 0.
    item.update(source='review-candidate:' + kind, confidence=min(confidence, .69),
                warning=WARNING, review_only=True)
    return item


def person_rows(items):
    candidates = [{**item, 'kind': 'application_person'} if item.get('kind') == 'operative_person' else item
                  for item in items]
    task = SimpleNamespace(scenario='person_card', recognized={'_review_suggestions_v1': candidates}, values={})
    return review_fields_with_occurrence(task)


def person_fields(item):
    rows, _ = person_rows([item])
    kind = item['kind']
    result = {}
    for row in rows:
        if row.attr in FIELDS and row.value:
            result[row.attr] = uncertain({k: v for k, v in asdict(row).items() if k not in {'attr','title','choices','required','evidence_status'}}, kind)
    parts = {row.attr: row for row in rows if row.attr in {'last_name','first_name','middle_name'} and row.value}
    raw = item.get('fields', {}).get('fio') or item.get('fields', {}).get('fio_raw')
    if len(parts) == 3:
        name = ' '.join(parts[k].value for k in ('last_name','first_name','middle_name'))
        source = raw if isinstance(raw, dict) else asdict(parts['last_name'])
        result['fio_raw'] = uncertain({**source, 'value': name}, kind)
    elif value(raw):
        result['fio_raw'] = uncertain(raw, kind)
    # Research review must expose the observed incomplete address as incomplete,
    # rather than hiding the evidence needed to report a missing apartment.
    for name in ('address', 'address_kind'):
        candidate = item.get('fields', {}).get(name)
        if name not in result and isinstance(candidate, dict) and value(candidate):
            result[name] = uncertain(candidate, kind)
            result[name].update(warning='Неполный адрес кандидата OCR; проверьте все компоненты по оригиналу.', address_partial=True)
    return result


def options(observed):
    result = []
    seen = set()
    suggestions = observed.get('review_suggestions', [])
    if not isinstance(suggestions, list):
        return result
    for item in suggestions[:32]:
        if not isinstance(item, dict) or not isinstance(item.get('fields'), dict):
            continue
        kind = item.get('kind')
        if kind in {'application_person', 'operative_person'}:
            fields = person_fields(item)
            entity = 'person'
            identity = normalized(value(fields.get('fio_raw')))
        elif kind == 'entity_candidate' or kind in PARTICIPANTS:
            entity = value(item['fields'].get('entity_kind'))
            if entity not in {'legal_entity', 'sole_proprietor'}:
                continue
            target = 'legal_name' if entity == 'legal_entity' else 'debtor_name'
            copyable = item.get('copyable_fields', [])
            allowed = set(copyable if isinstance(copyable, list) else []) | {'entity_kind', target}
            fields = {name: uncertain(field, kind) for name, field in item['fields'].items()
                      if name in FIELDS and name in allowed and isinstance(field, dict) and value(field)}
            identity = normalized(value(fields.get(target)))
        else:
            continue
        if not identity:
            continue
        identifier = digest(item)
        if identifier in seen:
            continue
        seen.add(identifier)
        result.append({'id': identifier, 'kind': kind, 'entity_kind': entity, 'fields': fields,
                       'role': (item.get('role') if item.get('role') in ROLES else ('debtor' if kind == 'entity_candidate' else None)),
                       'role_location': deepcopy(item.get('role_location')),
                       'role_warning': 'Роль должника не установлена; показаны сведения об участнике дела.' if kind in PARTICIPANTS else WARNING,
                       'warning': WARNING, '_identity': identity, '_source': item})
    return result


def weak_party_type(fields):
    """A type inferred solely from a civil counterparty is not a debtor anchor."""
    return (fields.get('entity_kind', {}).get('source') == 'debtor_entity_candidate:v1'
            and not any(value(fields.get(name)) for name in ('fio_raw','legal_name','debtor_name')))


def compatible_with_current(option, fields):
    kind = value(fields.get('entity_kind'))
    if weak_party_type(fields):
        kind = None
    if kind in KINDS and kind != option['entity_kind']:
        return False
    target = {'person': 'fio_raw', 'legal_entity': 'legal_name', 'sole_proprietor': 'debtor_name'}[option['entity_kind']]
    if value(fields.get(target)) and normalized(value(fields[target])) != option['_identity']:
        return False
    relevant = set(applicable_fields(option['entity_kind'])) if kind in KINDS else set(FIELDS)
    for other_name in ('fio_raw', 'legal_name', 'debtor_name'):
        if other_name in relevant and other_name != target and value(fields.get(other_name)):
            return False
    for name in ('inn', 'snils', 'ogrn', 'ogrnip', 'kpp', 'address', 'address_kind', *NAME_PARTS):
        if name not in relevant:
            continue
        current = value(fields.get(name))
        proposed = value(option['fields'].get(name))
        if current and (not proposed or normalized(current) != normalized(proposed)):
            return False
    return True


def choose(items):
    if not items or len({(x['entity_kind'], x['_identity'], x.get('role')) for x in items}) != 1:
        return None
    if items[0]['entity_kind'] == 'person':
        _rows, occurrence = person_rows([item['_source'] for item in items])
        chosen = items[occurrence[0]] if occurrence else None
    else:
        # Equivalent occurrences can share values; conflicting ones require
        # explicit owner selection. This order never resolves conflicting data.
        chosen = max(items, key=lambda x: len(x['fields']))
    if chosen is None:
        return None
    for item in items:
        for name, field in item['fields'].items():
            proposed = value(chosen['fields'].get(name))
            other = value(field)
            if name == 'address' and proposed and other and proposed != other:
                a, b = normalized(proposed), normalized(other)
                if a.startswith(b) and re.fullmatch(r'\s*,?\s*кв(?:артира)?\.?\s*№?\s*\d+[а-яёa-z]?(?:[/\-]\d+)?', a[len(b):], re.I):
                    continue
            if other and proposed and normalized(other) != normalized(proposed):
                return None
            if other and not proposed:
                return None
    return chosen


def project(observed, effective, *, pending=True):
    fields = deepcopy(effective)
    owned_fio = fields.get('fio_raw', {})
    if owned_fio.get('source') == 'owner-feedback' and not owned_fio.get('derived_from_fields'):
        parts = (value(owned_fio) or '').split()
        for index, name in enumerate(NAME_PARTS):
            fields[name] = {'value': parts[index] if len(parts) == 3 else None, 'source': 'owner-feedback',
                            'confidence': 0., 'warning': 'Разнесение проверенного ФИО; проверьте компоненты.', 'source_start': None, 'source_end': None, 'source_page': None, 'source_boxes': []}
    candidates = options(observed) if pending else []
    eligible = [x for x in candidates if compatible_with_current(x, fields)]
    explicit_debtor = any(value(fields.get(name)) and fields[name].get('source') in {'debtor_role:v1', 'role_partial', 'debtor_entity_role:v1'}
                          for name in ('fio_raw','legal_name','debtor_name'))
    automatic = [x for x in eligible if x['kind'] != 'caption_participant'
                 and (not explicit_debtor or x['kind'] not in PARTICIPANTS)]
    debtors = [x for x in automatic if x.get('role') == 'debtor']
    defendants = [x for x in automatic if x.get('role') == 'defendant']
    persons = [x for x in automatic if x['kind'] == 'application_person']
    civil = [x for x in automatic if x['kind'] == 'entity_participant']
    # Preserve the existing coherent person review subject when the additional
    # companies are only petitioning/plaintiff parties, not candidate debtors or
    # defendants. All corporate occurrences remain visible/selectable.
    person_preference = persons and civil and all(x.get('role') in {'plaintiff','applicant'} for x in civil)
    selected = choose(debtors or defendants or (persons if person_preference else automatic))
    if selected is None and not explicit_debtor and not debtors and not defendants:
        selected = choose([x for x in eligible if x['kind'] == 'caption_participant'])
    if selected and selected['entity_kind'] == 'person' and weak_party_type(fields):
        fields['entity_kind'] = uncertain({'value': 'person'}, selected['kind'])
    elif selected is None and weak_party_type(fields) and len({x['entity_kind'] for x in eligible}) > 1:
        fields['entity_kind'] = {'value': None, 'source': 'review-derived:mixed-subject-types',
                                 'confidence': 0., 'warning': 'Тип зависит от выбора участника; выберите явно.', 'review_only': True}
    if selected:
        for name, candidate in selected['fields'].items():
            if not value(fields.get(name)) and fields.get(name, {}).get('source') != 'owner-feedback':
                fields[name] = deepcopy(candidate)
    kind = value(fields.get('entity_kind'))
    if kind not in KINDS:
        kind = None
        owner_cleared = fields.get('entity_kind', {}).get('source') == 'owner-feedback'
        if selected and not owner_cleared:
            kind = selected['entity_kind']
            fields['entity_kind'] = uncertain({'value': kind}, selected['kind'])
        elif not owner_cleared and value(fields.get('fio_raw')) and fields['fio_raw'].get('source') in {'debtor_role:v1', 'role_partial', 'owner-feedback'}:
            kind = 'person'
            fields['entity_kind'] = {'value': kind, 'source': 'review-derived:debtor-person', 'confidence': 0.,
                                     'warning': 'Тип определён из сведений о физическом лице; проверьте по оригиналу.', 'review_only': True}
        elif not owner_cleared and candidates and {x['entity_kind'] for x in candidates} == {'legal_entity'}:
            kind = 'legal_entity'
            fields['entity_kind'] = uncertain({'value': kind}, 'entity_participant')
    subject_role = 'debtor' if explicit_debtor else selected.get('role') if selected else None
    subject_warning = (selected.get('role_warning') if selected else None) or (WARNING if selected else 'Выберите участника дела; должник не установлен.' if candidates and not explicit_debtor else None)
    public = [{k: v for k, v in item.items() if not k.startswith('_')} for item in candidates]
    return {'version': PROJECTION_VERSION, 'entity_kind': kind, 'applicable_fields': applicable_fields(kind),
            'fields': fields, 'candidates': public, 'selected_candidate_id': selected['id'] if selected else None,
            'selection_required': bool(eligible and not selected and not explicit_debtor),
            'subject_role': subject_role, 'subject_is_debtor': subject_role == 'debtor',
            'subject_role_location': deepcopy(selected.get('role_location')) if selected else None,
            'subject_warning': subject_warning,
            'warning': WARNING if selected else ('Есть неоднозначные кандидаты; выберите участника явно.' if eligible else None)}


def _expert_evidence(field, variants, *, variant=None):
    if not value(field):
        return False
    index, start, end = (field.get(key) for key in ('source_variant', 'source_start', 'source_end'))
    return (type(index) is int and 0 <= index < len(variants) and (variant is None or index == variant)
            and type(start) is int and type(end) is int and 0 <= start < end <= len(variants[index]))


def _expert_caption_metadata(variants, case_field, date_field):
    """Verify the parsed metadata and bind it to spans in the saved readings."""
    date_value = value(date_field)
    case_span = date_span = None
    for index, text in enumerate(variants):
        heading = _DECISION_TITLE.search(text, 0, 650) or _OTHER_ACT_TITLE.search(text, 0, 650)
        case = _EXPERT_CASE_LINE.search(text, heading.end(), heading.end() + 250) if heading else None
        if not case or normalize_case_number(case['number']) != value(case_field):
            return None
        anchored = [(start, end) for candidate, start, end in _date_candidates(text)
                    if candidate == date_value and 0 <= start - case.end() <= 180
                    and not text[case.end():start].strip() and '\n' not in text[case.end():start]]
        if len(anchored) != 1:
            return None
        if index == case_field['source_variant']:
            case_span = case.span('number')
        if index == date_field['source_variant']:
            date_span = anchored[0]
    return {
        'case_number': {**case_field, 'source_start': case_span[0], 'source_end': case_span[1]},
        'doc_date': {**date_field, 'source_start': date_span[0], 'source_end': date_span[1]},
    }


def _complete_expert_fields(kind, fields, variants, *, variant=None):
    required = EXPERT_FIELDS[kind]
    if not all(isinstance(fields.get(name), dict) and _expert_evidence(
            fields[name], variants, variant=variant if name not in {'case_number', 'doc_date'} else None,
    ) for name in required):
        return False
    address_kind = fields.get('address_kind')
    address_types = {'registration'} if kind == 'person' else {'registration', 'postal', 'actual'}
    if (fields['address'].get('address_partial') or not isinstance(address_kind, dict)
            or not _expert_evidence(address_kind, variants, variant=variant)
            or value(address_kind) not in address_types):
        return False
    inn = value(fields['inn'])
    return (valid_inn(inn) and len(digits(inn)) == (10 if kind == 'legal_entity' else 12)
            and {'person': valid_snils, 'legal_entity': valid_ogrn, 'sole_proprietor': valid_ogrnip}[kind](
                value(fields[required[2]]),
            ))


def _expert_card(kind, fields):
    required = EXPERT_FIELDS[kind]
    primary = {name: {k: v for k, v in fields[name].items()
                      if k in {'value', 'source', 'source_variant', 'source_start', 'source_end', 'source_page'}}
               for name in required}
    return {'version': EXPERT_VERSION, 'entity_kind': kind, 'subject_role': 'debtor', 'subject_is_debtor': True,
            'applicable_fields': list(required), 'fields': primary}


def _expert_bankruptcy_caption(text, kind, subject, disposition_start):
    from sro_robot.core.entity_extract import IP_PREFIX, LEGAL_PREFIX, _quoted_name_end

    title = _DECISION_TITLE.search(text, 0, 650) or _OTHER_ACT_TITLE.search(text, 0, 650)
    hearing = _EXPERT_HEARING.search(text, title.end(), 2500) if title else None
    findings = _EXPERT_FINDINGS.search(text, hearing.end(), min(3000, disposition_start)) if hearing else None
    if not findings:
        return False
    caption = text[hearing.end():findings.start()]
    if ('\f' in caption or _CIVIL_CAPTION.search(caption) or _REFERENCED_DISPOSITION.search(caption)
            or not _BANKRUPTCY_CAPTION.search(caption)):
        return False
    for pattern in (INSOLVENCY_CASE_NAME, BANKRUPTCY_PERSON_NAME, _CAPTION_PERSON, _CAPTION_SELF_APPLICATION):
        for named in pattern.finditer(caption):
            name = NAME_TRIPLE.fullmatch(named['name'])
            if kind != 'person' or not name or _role_identity_key(' '.join(name.groups())) != _role_identity_key(subject):
                return False
    tails = [caption[marker.end():] for marker in _CAPTION_SUBJECT.finditer(caption)]
    for marker in _EXPLICIT_DEBTOR_LINE.finditer(text, title.end(), disposition_start):
        tail = text[marker.end():disposition_start].partition('\n')[0].rstrip('\r')
        if named := NAME_TRIPLE.match(tail):
            if kind != 'person' or _role_identity_key(' '.join(named.groups())) != _role_identity_key(subject):
                return False
        tails.append(tail)
    for tail in tails:
        if IP_PREFIX.match(tail) and kind != 'sole_proprietor':
            return False
        if LEGAL_PREFIX.match(tail):
            end = _quoted_name_end(tail)
            if kind != 'legal_entity' or end is None or normalized(tail[:end]) != normalized(subject):
                return False
    return True


def expert_projection(captured):
    """Admit an exact, complete disposition result for binary expert review."""
    if not captured:
        return None, 'needs_extraction'
    variants = captured.get('variants', [])
    if (not isinstance(variants, list) or not 1 <= len(variants) <= 5
            or not all(isinstance(text, str) and text.strip() for text in variants)):
        return None, 'needs_extraction'
    if not any(_BANKRUPTCY_TERMS.search(text) for text in variants):
        return None, 'outside_scope'
    observed = captured['observed_pipeline']
    suggestions = observed.get('review_suggestions', [])
    if not isinstance(suggestions, list):
        return None, 'needs_extraction'
    decisions = [item for item in suggestions if isinstance(item, dict)
                 and item.get('kind') in {'operative_person', 'entity_candidate'}
                 and item.get('role') == 'debtor' and isinstance(item.get('role_location'), dict)]
    case_fields = {name: observed.get(name) for name in ('case_number', 'doc_date')}
    if not all(isinstance(field, dict) and _expert_evidence(field, variants) for field in case_fields.values()):
        return None, 'needs_extraction'
    try:
        date(*reversed([int(part) for part in value(case_fields['doc_date']).split('.')]))
    except (TypeError, ValueError):
        return None, 'needs_extraction'
    if case_fields['case_number'].get('warning'):
        return None, 'needs_extraction'
    case_fields = _expert_caption_metadata(variants, case_fields['case_number'], case_fields['doc_date'])
    if not case_fields:
        return None, 'needs_extraction'
    if not decisions:
        return None, 'needs_extraction'
    candidates = options({'review_suggestions': decisions})
    if len(candidates) != len(variants) or len({item['entity_kind'] for item in candidates}) != 1:
        return None, 'needs_extraction'
    kind = candidates[0]['entity_kind']
    required = EXPERT_FIELDS[kind]
    actual = {}
    for item in candidates:
        role = item['role_location']
        index = role.get('source_variant')
        if not _expert_evidence({**role, 'value': 'debtor'}, variants) or index in actual:
            return None, 'needs_extraction'
        opening = role['source_start']
        if _REFERENCED_DISPOSITION.search(variants[index], max(0, opening - 250), opening):
            return None, 'needs_extraction'
        fields = {**item['fields'], **case_fields}
        if not _complete_expert_fields(kind, fields, variants, variant=index):
            return None, 'needs_extraction'
        if not _expert_bankruptcy_caption(variants[index], kind, value(fields[required[0]]), opening):
            return None, 'needs_extraction'
        for name in required[:4]:
            field = fields[name]
            if item['kind'] == 'operative_person':
                bound = role['source_start'] <= field['source_start'] < field['source_end'] <= role['source_end']
            else:
                bound = role['source_end'] <= field['source_start'] < field['source_end'] <= role['source_end'] + 450
            if not bound:
                return None, 'needs_extraction'
            if name in {required[1], required[2]} and digits(
                variants[index][field['source_start']:field['source_end']]
            ) != digits(value(field)):
                return None, 'needs_extraction'
        actual[index] = {name: fields[name] for name in required}
    if len({tuple(normalized(value(fields[name])) for name in required) for fields in actual.values()}) != 1:
        return None, 'needs_extraction'
    for native_kind, scope in EXPERT_FIELDS.items():
        native_name = observed.get(scope[0])
        if (isinstance(native_name, dict) and native_name.get('source') == _EXPERT_ROLE_SOURCES[native_kind]
                and value(native_name) and (native_kind != kind
                     or normalized(value(native_name)) != normalized(value(actual[0][required[0]])))):
            return None, 'needs_extraction'
    return _expert_card(kind, actual[0]), 'ready'
