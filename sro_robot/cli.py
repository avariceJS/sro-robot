"""Command-line entry points for local extraction and payload validation.

Real SRO writes are disabled in this build. `run` performs local validation;
the legacy `--live` option is retained only to fail explicitly and safely.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .core.models import DebtorCard, confidence_at_least
from .documents import get_profile
from .scenarios import DEFAULT_SCENARIO, get_scenario

console = Console()

SHOWN_FIELDS = (
    ("Тип документа", "doc_type"),
    ("Дата документа", "doc_date"),
    ("Номер дела", "case_number"),
    ("Суд", "court"),
    ("Фамилия", "last_name"),
    ("Имя", "first_name"),
    ("Отчество", "middle_name"),
    ("ИНН", "inn"),
    ("СНИЛС", "snils"),
    ("Адрес", "address"),
    ("Тип адреса", "address_kind"),
)

KIND_RU = {"registration": "адрес регистрации", "actual": "фактический адрес"}

def show_card(card: DebtorCard, missing: list[str]) -> None:
    table = Table(title="Распознанные поля", show_lines=False)
    table.add_column("Поле", style="bold")
    table.add_column("Значение")
    table.add_column("Точн.", justify="right")
    table.add_column("Проверить", style="yellow")

    for title, attr in SHOWN_FIELDS:
        field = getattr(card, attr)
        value = field.value or "—"
        if attr == "address_kind":
            value = KIND_RU.get(field.value, value)
        color = (
            "green" if confidence_at_least(field.confidence)
            else "yellow" if confidence_at_least(field.confidence, 0.5)
            else "red"
        )
        table.add_row(title, f"[{color}]{value}[/{color}]", f"{field.confidence:.2f}", field.warning or "")

    console.print(table)
    console.print(f"[bold]В карточку СРО уйдёт ИНН:[/bold] {card.inn_or_case}")

    if missing:
        console.print(Panel(f"Не найдены обязательные поля: {', '.join(missing)}", style="red"))


def cmd_extract(args) -> int:
    card = get_profile(args.profile).parse(args.file, dpi=args.dpi)
    show_card(card, get_scenario(args.scenario).validate(card))

    out = Path(args.out) if args.out else Path("out") / (Path(args.file).stem + ".json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(card.to_json(), encoding="utf-8")
    console.print(f"JSON сохранён: {out}")

    if args.show_text:
        console.print(Panel(card.raw_text[:3000], title="Текст OCR"))
    return 0


def cmd_run(args) -> int:
    scenario = get_scenario(args.scenario)
    card = get_profile(args.profile).parse(args.file, dpi=args.dpi)

    missing = scenario.validate(card)
    show_card(card, missing)
    if missing:
        console.print("[red]Останавливаюсь: не хватает обязательных полей.[/red]")
        return 1

    if args.live:
        console.print(
            "[red]Запись в реальный СРО отключена. Доступна только локальная проверка.[/red]"
        )
        return 2

    result = {
        "execution_mode": "local_validation",
        "saved": False,
        "missing_required_fields": [],
    }
    console.print(Panel(json.dumps(result, ensure_ascii=False, indent=2), title="Итог"))
    console.print("[yellow]Локальная проверка завершена; в СРО запросы не выполнялись.[/yellow]")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sro_robot", description="Прототип автоматизации карточек должников")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_extract = sub.add_parser("extract", help="распознать документ и показать поля")
    p_extract.add_argument("file")
    p_extract.add_argument("--dpi", type=int, default=300)
    p_extract.add_argument("--out")
    p_extract.add_argument("--show-text", action="store_true")
    p_extract.add_argument("--profile", default="court_ruling", help="профиль извлечения")
    p_extract.add_argument("--scenario", default=DEFAULT_SCENARIO, help="чьи обязательные поля проверять")
    p_extract.set_defaults(func=cmd_extract)

    p_run = sub.add_parser("run", help="локально распознать и проверить документ")
    p_run.add_argument("file")
    p_run.add_argument("--dpi", type=int, default=300)
    p_run.add_argument("--live", action="store_true", help="отключено: реальная запись запрещена")
    p_run.add_argument("--profile", default="court_ruling", help="профиль извлечения")
    p_run.add_argument("--scenario", default=DEFAULT_SCENARIO, help="какой сценарий запускать")
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
