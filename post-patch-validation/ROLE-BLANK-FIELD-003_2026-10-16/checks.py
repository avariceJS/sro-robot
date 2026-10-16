"""Synthetic, side-blind checks for blank-line debtor-field continuation."""

from __future__ import annotations

import json
import sys

from sro_robot.core.extract import find_address, parse


def reached() -> None:
    print("PPV_REACHED", flush=True)


def control() -> None:
    card = parse(["Должник: Иванова Анна Сергеевна, ИНН 772881234508"])
    assert card.fio_raw.source == "debtor_role:v1"
    assert card.inn.value == "772881234508"
    print("control:inline-debtor-fields")


def exploit() -> None:
    card = parse([
        "Должник: Иванова Анна Сергеевна, ИНН 772881234508",
        "Должник: Иванова Анна Сергеевна\n\nИНН 772881234508",
    ])
    reached()
    assert card.inn.value == "772881234508"
    assert card.inn.source == "debtor_role:v1" and card.inn.warning is None


def variant_address_warning() -> None:
    card = parse([
        "Должник: Иванова Анна Сергеевна, ИНН 772881234508\n\n"
        "Адрес регистрации: 123456, город Примерск"
    ])
    reached()
    assert card.address.value is None
    assert card.address.source == "role_ambiguous" and card.address.warning


def variant_address_corroboration() -> None:
    address = "Адрес регистрации: 123456, г. Примерск, ул. Макетная, д. 7"
    card = parse([
        "Должник: Иванова Анна Сергеевна, ИНН 772881234508, " + address,
        "Должник: Иванова Анна Сергеевна, ИНН 772881234508\n\n" + address,
    ])
    reached()
    assert card.address.value == "123456, г. Примерск, ул. Макетная, д. 7"
    assert card.address.source == "debtor_role:v1" and card.address.warning is None


def variant_wrapped_address() -> None:
    card = parse([
        "Должник: Иванова Анна Сергеевна, ИНН 772881234508\n\n"
        "Адрес регистрации: 101000, вымышленный город Макетны\n\n"
        "й, улица Учебная, дом 1"
    ])
    reached()
    assert card.address.value == "101000, вымышленный город Макетный, улица Учебная, дом 1"
    assert card.address.source == "role_partial" and card.address.warning


def behavior() -> None:
    print(json.dumps(find_address("адрес регистрации: 123456, г. Примерск, ул. Макетная, д. 7"),
                     ensure_ascii=False, separators=(",", ":")))


def regression() -> None:
    card = parse([
        "Должник: Иванова Анна Сергеевна, ИНН 772881234508",
        "Должник: Иванова Анна Сергеевна",
    ])
    assert card.inn.value == "772881234508" and card.inn.source == "role_partial"
    assert card.inn.warning
    print("regression:genuine-ocr-miss-review")


def security() -> None:
    for later in (
        "Заявитель: Петров Петр Петрович, ИНН 772881234508",
        "Представитель должника: Петров Петр Петрович, ИНН 772881234508",
        "Петров Петр Петрович\nИНН 772881234508",
        "772881234508",
    ):
        card = parse(["Должник: Иванова Анна Сергеевна\n\n" + later])
        assert card.inn.value is None
    print("security:new-or-unlabeled-person-not-spliced")


CASES = {
    "control": control,
    "exploit": exploit,
    "variant_address_warning": variant_address_warning,
    "variant_address_corroboration": variant_address_corroboration,
    "variant_wrapped_address": variant_wrapped_address,
    "behavior": behavior,
    "regression": regression,
    "security": security,
}

if __name__ == "__main__":
    CASES[sys.argv[1]]()
