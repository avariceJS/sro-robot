"""Conservative destination for one locally recognized source document.

The route describes the staff worklist, not approval, a persisted debtor card,
an assignment, or an external SRO operation. It never infers participant role
from an identifier, a case number, or a review-only person suggestion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .entity_extract import EntityDebtorCard, ENTITY_ROLE_SOURCE
from .models import DebtorCard


@dataclass(frozen=True)
class MixedRoute:
    registry: str
    subject_kind: str | None
    reason: str

    def as_dict(self) -> dict[str, str | None]:
        return {
            "registry": self.registry,
            "subject_kind": self.subject_kind,
            "reason": self.reason,
        }


REVIEW = MixedRoute("review", None, "participant_role_uncertain")


def _field_value(card: object, name: str) -> str:
    return str(getattr(getattr(card, name, None), "value", "") or "").strip()


def _field_source(card: object, name: str) -> str:
    return str(getattr(getattr(card, name, None), "source", "") or "")


def _applicant_is_bound(card: object, evidence: Mapping[str, object] | None) -> bool:
    if not isinstance(evidence, Mapping):
        return False
    if evidence.get("role_source") != "applicant_role:v1":
        return False
    name = evidence.get("name")
    case_number = evidence.get("case_number")
    if not isinstance(name, str) or not name.strip():
        return False
    if not isinstance(case_number, str) or not case_number.strip():
        return False
    recognized_case = _field_value(card, "case_number")
    return bool(recognized_case) and recognized_case == case_number.strip()


def classify_card(
    card: DebtorCard | EntityDebtorCard,
    *,
    applicant_evidence: Mapping[str, object] | None = None,
) -> MixedRoute:
    """Classify source roles; retain missing or incomplete debtor roles for review.

    ``applicant_evidence`` is a separately extracted, role-bound occurrence.
    A person's ``application_person`` review suggestion is not that evidence:
    it can describe a debtor mentioned in an application. Callers must keep
    the role source and case binding when they introduce applicant extraction.
    """
    applicant = _applicant_is_bound(card, applicant_evidence)
    if isinstance(card, EntityDebtorCard):
        kind = _field_value(card, "entity_kind")
        typed_name = _field_value(card, "legal_name" if kind == "legal_entity" else "debtor_name")
        debtor_role_marked = _field_source(card, "entity_kind") == ENTITY_ROLE_SOURCE
        debtor = (
            debtor_role_marked
            and kind in {"legal_entity", "sole_proprietor"}
            and bool(typed_name)
        )
        if debtor:
            return MixedRoute("organizations", kind, "explicit_entity_debtor")
        if debtor_role_marked:
            return REVIEW
        if applicant:
            return MixedRoute("assignments", "applicant", "explicit_applicant")
        if kind in {"legal_entity", "sole_proprietor"} and typed_name:
            # A folder is a review destination. No debtor-card eligibility or
            # participant role follows from this tentative entity identity.
            return MixedRoute("organizations", kind, "review_entity_candidate")
    elif isinstance(card, DebtorCard):
        has_person_name = bool(_field_value(card, "last_name") and _field_value(card, "first_name"))
        debtor_role_marked = _field_source(card, "fio_raw") == "debtor_role:v1"
        debtor = (
            debtor_role_marked
            and has_person_name
        )
        if debtor:
            return MixedRoute("persons", "person", "explicit_person_debtor")
        if debtor_role_marked:
            return REVIEW
        if applicant:
            return MixedRoute("assignments", "applicant", "explicit_applicant")
        if has_person_name:
            return MixedRoute("persons", "person", "review_person_candidate")
        if any(
            isinstance(item, dict) and item.get("kind") == "application_person"
            for item in getattr(card, "review_suggestions", ())
        ):
            return MixedRoute("persons", "person", "review_person_candidate")
    return REVIEW
