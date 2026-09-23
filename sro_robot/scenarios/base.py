"""Общий контракт сценариев.

Сценарий — это инструкция «как из распознанного документа сделать запись в
СРО». Контракт одинаков для всех сценариев и не зависит от интерфейса: консоль
и будущий веб вызывают одни и те же методы и получают одни и те же структуры.

    spec = scenario.fields              # какие поля нужны на входе
    missing = scenario.validate(card)   # проверка данных, без браузера
    report = scenario.preflight(...)    # вход, реестр, поиск дублей — без записи
    result = scenario.run(...)          # запуск, результат со ссылкой на запись

Решение «создавать ли при найденных дублях» принимает вызывающая сторона:
сценарий только сообщает, что нашёл.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Protocol

from ..core.models import DebtorCard, Field as CoreField
from ..platform import Registry, SearchHit, SroClient


@dataclass(frozen=True)
class FieldSpec:
    """Поле карточки, которое сценарий кладёт в форму платформы.

    `choices` — пары «значение, подпись» для полей с фиксированным набором
    (тип адреса). По ним интерфейс рисует список вместо строки ввода.
    """

    attr: str
    title: str
    required: bool = False
    choices: tuple[tuple[str, str], ...] = ()

    def label_for(self, value: str | None) -> str:
        for option, label in self.choices:
            if option == value:
                return label
        return value or ""


@dataclass
class Preflight:
    """Что известно до записи: чего не хватает в данных и какие дубли нашлись."""

    missing: list[str] = field(default_factory=list)
    duplicates: list[SearchHit] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "missing": list(self.missing),
            "duplicates": [{"name": h.name, "url": h.card_url} for h in self.duplicates],
        }


class ReviewGate(Protocol):
    """Как сценарий спрашивает оператора, что делать с заполненной формой.

    Робот заполняет карточку за пару секунд, и без остановки человек не успевает
    ничего разглядеть. Реализацию даёт интерфейс: консоль спросит в терминале,
    веб — покажет снимки экрана и кнопки.
    """

    def ask(self, screenshots: list[str]) -> str:
        """Вернуть `"save"` — сохранить карточку, `"close"` — закрыть черновик."""


@dataclass
class ScenarioResult:
    """Итог запуска. `record_url` пуст при пробном прогоне: записи не появилось."""

    ok: bool = False
    saved: bool = False
    message: str = ""
    record_url: str = ""
    filled: dict[str, str] = field(default_factory=dict)
    screenshots: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "saved": self.saved,
            "message": self.message,
            "record_url": self.record_url,
            "filled": dict(self.filled),
            "screenshots": list(self.screenshots),
        }


class Scenario(ABC):
    name: str = ""
    title: str = ""
    fields: tuple[FieldSpec, ...] = ()
    list_path: str = ""
    selectors: dict[str, str] = {}
    result_link_key: str = "result_links"
    # Тип карточки на входе. У организации и назначения он будет свой.
    card_class: type = DebtorCard

    def card_from_values(self, values: dict[str, str], base: DebtorCard | None = None) -> DebtorCard:
        """Карточка с правками оператора: введённому человеком верим полностью."""
        card = deepcopy(base) if base is not None else self.card_class()
        for spec in self.fields:
            if spec.attr not in values:
                continue
            value = (values[spec.attr] or "").strip()
            current = getattr(card, spec.attr, None)
            if isinstance(current, CoreField) and value == (current.value or ""):
                continue
            setattr(card, spec.attr, CoreField(value or None, 1.0, "operator"))
        return card

    def validate(self, card: DebtorCard) -> list[str]:
        """Названия обязательных полей, которых не хватает. В сеть не ходит."""
        return [
            spec.title for spec in self.fields
            if spec.required and not getattr(card, spec.attr)
        ]

    def registry(self, client: SroClient) -> Registry:
        client.register_selectors(self.selectors)
        return Registry(client, self.list_path, self.result_link_key)

    @abstractmethod
    def preflight(self, client: SroClient, card: DebtorCard) -> Preflight:
        """Вход, открытие реестра и поиск дублей. Ничего не создаёт."""

    @abstractmethod
    def run(self, client: SroClient, card: DebtorCard,
            gate: ReviewGate | None = None) -> ScenarioResult:
        """Создание записи.

        Без `gate` поведение прежнее: при `client.dry_run` форма заполняется и
        закрывается, иначе сохраняется. С `gate` робот останавливается на
        заполненной форме и делает то, что ответит оператор.
        """
