"""Synthetic, side-blind checks for debtor address boundaries."""

from __future__ import annotations

import json
import sys

from sro_robot.core.extract import find_address, find_case_number, find_doc_date, parse


def reached() -> None:
    print("PPV_REACHED", flush=True)


def control() -> None:
    card = parse(["Должник: Иванова Анна Сергеевна, ИНН 772881234508"])
    assert card.fio_raw.value == "Иванова Анна Сергеевна"
    print("control:parser-live")


def exploit() -> None:
    card = parse([
        "Должник: Иванова Анна Сергеевна, ИНН 772881234508\n"
        "Адрес регистрации: 123456, г. Примерск, ул. Макетная, д. 7\n"
        "Документ содержит отдельный длинный абзац о порядке рассмотрения дела."
    ])
    reached()
    assert card.address.value == "123456, г. Примерск, ул. Макетная, д. 7"
    assert card.address.warning is None


def variant_multiline() -> None:
    card = parse([
        "Должник: Иванова Анна Сергеевна, ИНН 772881234508\n"
        "Адрес регистрации: 123456, г. Примерск,\n"
        "ул. Макетная, д. 7,\nкв. 42\n"
        "Документ содержит описание дела."
    ])
    reached()
    assert card.address.value == "123456, г. Примерск, ул. Макетная, д. 7, кв. 42"
    assert card.address.warning is None


def variant_undelimited() -> None:
    card = parse([
        "Должник: Иванова Анна Сергеевна, ИНН 772881234508\n"
        "Адрес регистрации: 123456, г. Примерск, ул. Макетная,\n"
        "Документ содержит дом 99 и иные сведения."
    ])
    reached()
    assert card.address.value is None and card.address.warning


def behavior() -> None:
    print(json.dumps(find_address("адрес регистрации: 123456, г. Примерск, ул. Макетная, д. 7"),
                     ensure_ascii=False, separators=(",", ":")))


def regression() -> None:
    assert find_case_number("Дело № A41-61776/2026") == "А41-61776/2026"
    assert find_doc_date("ОПРЕДЕЛЕНИЕ 14 августа 2026 года") == "14.08.2026"
    print("regression:case-and-date")


def security() -> None:
    # A document with no address marker must never gain one from stray prose.
    card = parse(["Должник: Иванова Анна Сергеевна. Дом 99 упомянут в другом контексте."])
    assert card.address.value is None
    print("security:no-address-marker")


CASES = {
    "control": control,
    "exploit": exploit,
    "variant_multiline": variant_multiline,
    "variant_undelimited": variant_undelimited,
    "behavior": behavior,
    "regression": regression,
    "security": security,
}

if __name__ == "__main__":
    CASES[sys.argv[1]]()
