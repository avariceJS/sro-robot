"""Owner-review projection of existing results, never authoritative extraction.

The maintained office occurrence projector supplies person candidates. This
adapter adds the research card schema and refuses conflicting identity bundles.
No OCR, application settings, database engine or external record is used.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math
import re
from types import SimpleNamespace

from app.services.fields import review_fields_with_occurrence
from .review_schema import FIELDS, NAME_PARTS

PROJECTION_VERSION = 'owner-review-card:v2'
KINDS = {'person', 'legal_entity', 'sole_proprietor'}
COMMON = ('entity_kind', 'case_number', 'doc_date', 'court', 'doc_type')
PERSON = NAME_PARTS + ('inn', 'snils', 'address', 'address_kind')
LEGAL = ('legal_name', 'short_name', 'legal_form', 'inn', 'ogrn', 'kpp', 'address', 'address_kind')
IP = ('debtor_name', 'inn', 'ogrnip', 'address', 'address_kind')
PARTY = set(FIELDS) - set(COMMON)
WARNING = 'Кандидат OCR для проверки; роль и принадлежность выбранному участнику подтвердите по оригиналу.'
ROLES = {'debtor', 'defendant', 'plaintiff', 'applicant', 'interested_party'}


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
    task = SimpleNamespace(scenario='person_card', recognized={'_review_suggestions_v1': items}, values={})
    return review_fields_with_occurrence(task)


def person_fields(item):
    rows, chosen = person_rows([item])
    result = {}
    for row in rows:
        if row.attr in FIELDS and row.value:
            result[row.attr] = uncertain({k: v for k, v in asdict(row).items() if k not in {'attr','title','choices','required','evidence_status'}}, 'application_person')
    parts = {row.attr: row for row in rows if row.attr in {'last_name','first_name','middle_name'} and row.value}
    raw = item.get('fields', {}).get('fio') or item.get('fields', {}).get('fio_raw')
    if len(parts) == 3:
        name = ' '.join(parts[k].value for k in ('last_name','first_name','middle_name'))
        source = raw if isinstance(raw, dict) else asdict(parts['last_name'])
        result['fio_raw'] = uncertain({**source, 'value': name}, 'application_person')
    elif value(raw):
        result['fio_raw'] = uncertain(raw, 'application_person')
    # Research review must expose the observed incomplete address as incomplete,
    # rather than hiding the evidence needed to report a missing apartment.
    for name in ('address', 'address_kind'):
        candidate = item.get('fields', {}).get(name)
        if name not in result and isinstance(candidate, dict) and value(candidate):
            result[name] = uncertain(candidate, 'application_person')
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
        if kind == 'application_person':
            fields = person_fields(item)
            entity = 'person'
            identity = normalized(value(fields.get('fio_raw')))
        elif kind in {'entity_candidate', 'entity_participant'}:
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
                       'role_warning': 'Роль должника не установлена; показаны сведения об участнике дела.' if kind == 'entity_participant' else WARNING,
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
        chosen = next((item for item in items if occurrence and digest(occurrence[1]) == item['id']), None)
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
    automatic = [x for x in eligible if x['kind'] != 'entity_participant'] if explicit_debtor else eligible
    debtors = [x for x in automatic if x.get('role') == 'debtor']
    defendants = [x for x in automatic if x.get('role') == 'defendant']
    persons = [x for x in automatic if x['kind'] == 'application_person']
    civil = [x for x in automatic if x['kind'] == 'entity_participant']
    # Preserve the existing coherent person review subject when the additional
    # companies are only petitioning/plaintiff parties, not candidate debtors or
    # defendants. All corporate occurrences remain visible/selectable.
    person_preference = persons and civil and all(x.get('role') in {'plaintiff','applicant'} for x in civil)
    selected = choose(debtors or defendants or (persons if person_preference else automatic))
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
