"""Сценарии: инструкции «документ → запись в СРО» с единым контрактом.

Интерфейс (консоль, дальше веб) выбирает сценарий по имени и не знает, что
именно тот заполняет.
"""

from __future__ import annotations

from .base import FieldSpec, Preflight, Scenario, ScenarioResult
from .person_card import PersonCardScenario

SCENARIOS: dict[str, Scenario] = {
    PersonCardScenario.name: PersonCardScenario(),
}

DEFAULT_SCENARIO = PersonCardScenario.name


def get_scenario(name: str = DEFAULT_SCENARIO) -> Scenario:
    try:
        return SCENARIOS[name]
    except KeyError:
        known = ", ".join(sorted(SCENARIOS))
        raise KeyError(f"Неизвестный сценарий «{name}». Известные: {known}") from None


def all_scenarios() -> list[Scenario]:
    return list(SCENARIOS.values())


__all__ = [
    "DEFAULT_SCENARIO",
    "FieldSpec",
    "PersonCardScenario",
    "Preflight",
    "SCENARIOS",
    "Scenario",
    "ScenarioResult",
    "all_scenarios",
    "get_scenario",
]
