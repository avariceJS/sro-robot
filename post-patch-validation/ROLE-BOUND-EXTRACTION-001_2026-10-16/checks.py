"""Synthetic, side-blind role extraction checks for archive validation."""

from __future__ import annotations

import json
import sys

from sro_robot.core.extract import find_address, find_case_number, find_doc_date, parse


def reached() -> None:
    print("PPV_REACHED", flush=True)


def control() -> None:
    card = parse(["Должник: Иванов Иван Иванович, ИНН 772881234508"])
    assert card.fio_raw.value == "Иванов Иван Иванович"
    print("control:role-parser-live")


def exploit() -> None:
    card = parse([
        "Заявитель: Иванова Анна Сергеевна, СНИЛС 112-233-445 95, "
        "адрес регистрации: 123456, г. Примерск, ул. Тестовая, д. 7. "
        "Должник: Петров Петр Петрович, ИНН 772881234508"
    ])
    reached()
    assert card.snils.value is None and card.address.value is None


def variant_ocr() -> None:
    card = parse([
        "Должник: Иванов Иван Иванович, ИНН 772881234508",
        "Должник: Петров Петр Петрович, ИНН 500100732259",
    ])
    reached()
    assert card.fio_raw.value is None and card.inn.value is None


def variant_declension() -> None:
    card = parse(["Должник: Сидоровой Анны Сергеевны, ИНН 772881234508"])
    reached()
    assert card.last_name.warning and card.first_name.warning and card.middle_name.warning


def behavior() -> None:
    print(json.dumps(find_address("адрес регистрации: 123456, г. Примерск, ул. Макетная, д. 7"),
                     ensure_ascii=False, separators=(",", ":")))


def regression() -> None:
    assert find_case_number("Дело № A41-61776/2026") == "А41-61776/2026"
    assert find_doc_date("ОПРЕДЕЛЕНИЕ 14 августа 2026 года") == "14.08.2026"
    print("regression:case-and-date")


def security() -> None:
    card = parse([
        "Заявитель Петрова Мария Ивановна, ИНН 772881234508. "
        "Должник: ООО Макет"
    ])
    assert card.fio_raw.value is None and card.inn.value is None
    print("security:organization-debtor-not-person")


CASES = {
    "control": control,
    "exploit": exploit,
    "variant_ocr": variant_ocr,
    "variant_declension": variant_declension,
    "behavior": behavior,
    "regression": regression,
    "security": security,
}

if __name__ == "__main__":
    CASES[sys.argv[1]]()
