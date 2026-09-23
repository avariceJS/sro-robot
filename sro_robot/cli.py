"""Командная строка прототипа.

    python -m sro_robot extract samples/opredelenie_1.jpg
    python -m sro_robot inspect
    python -m sro_robot run samples/opredelenie_1.jpg            # пробный прогон
    python -m sro_robot run samples/opredelenie_1.jpg --live      # с сохранением

Консоль — только интерфейс: проверку данных, поиск дублей и заполнение формы
делает сценарий, поэтому тот же контракт сможет вызвать веб.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import Config
from .core.models import DebtorCard
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
    ("Дата рождения", "birth_date"),
    ("Место рождения", "birth_place"),
    ("ИНН", "inn"),
    ("СНИЛС", "snils"),
    ("Адрес", "address"),
    ("Тип адреса", "address_kind"),
)

KIND_RU = {"registration": "адрес регистрации", "actual": "фактический адрес"}

REGISTRY_CHOICES = {
    "person": ("/register/person_list.aspx", "form_map.json"),
    "firm": ("/register/firm_list.aspx", "form_map_firm.json"),
    "assignment": ("/register/assignment_list.aspx", "form_map_assignment.json"),
}


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
        color = "green" if field.confidence >= 0.7 else "yellow" if field.confidence >= 0.5 else "red"
        table.add_row(title, f"[{color}]{value}[/{color}]", f"{field.confidence:.2f}", field.warning or "")

    console.print(table)
    console.print(f"[bold]В карточку СРО уйдёт ИНН:[/bold] {card.inn_or_case}")

    if missing:
        console.print(Panel(f"Не найдены обязательные поля: {', '.join(missing)}", style="red"))


class ConsoleReviewGate:
    """Останавливает робота на заполненной форме и спрашивает оператора."""

    def ask(self, screenshots: list[str]) -> str:
        if screenshots:
            console.print(f"Снимки формы: {', '.join(screenshots)}")
        console.print("[bold]Форма заполнена, окно браузера открыто. Посмотрите её.[/bold]")
        answer = console.input(
            "Сохранить карточку в СРО? [y] сохранить, Enter — закрыть без записи: "
        ).strip().lower()
        return "save" if answer in ("y", "д", "yes", "да") else "close"


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


def cmd_inspect(args) -> int:
    from .platform.inspect_form import run

    list_path, out_name = REGISTRY_CHOICES[args.registry]
    run(Config.load(), list_path=list_path, out_name=out_name)
    return 0


def cmd_run(args) -> int:
    from .platform import SroClient, SroError

    scenario = get_scenario(args.scenario)
    card = get_profile(args.profile).parse(args.file, dpi=args.dpi)

    missing = scenario.validate(card)
    show_card(card, missing)
    if missing:
        console.print("[red]Останавливаюсь: не хватает обязательных полей.[/red]")
        return 1

    if not args.yes:
        answer = console.input("Данные верны? Продолжить в СРО? [y/N] ").strip().lower()
        if answer not in ("y", "д", "yes", "да"):
            console.print("Отменено.")
            return 1

    cfg = Config.load()
    with SroClient(cfg, dry_run=not args.live) as client:
        try:
            report = scenario.preflight(client, card)
            if report.duplicates:
                console.print(Panel(
                    "\n".join(h.name for h in report.duplicates[:10]),
                    title=f"Найдено записей: {len(report.duplicates)} — карточка уже может существовать",
                    style="yellow",
                ))
                if not args.force:
                    console.print("Создание пропущено. Повторите с --force, если это другой человек.")
                    return 0

            result = scenario.run(client, card, gate=ConsoleReviewGate() if args.preview else None)
            console.print(Panel(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), title="Итог"))
            if not args.live:
                console.print("[yellow]Пробный прогон: ничего не сохранено. Для записи добавьте --live[/yellow]")
        except SroError as exc:
            console.print(Panel(str(exc), title="Ошибка робота", style="red"))
            try:
                console.print(f"Скриншот: {client.shot('error')}")
            except Exception:
                pass
            return 2
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

    p_inspect = sub.add_parser("inspect", help="снять карту полей формы СРО")
    p_inspect.add_argument("--registry", choices=sorted(REGISTRY_CHOICES), default="person")
    p_inspect.set_defaults(func=cmd_inspect)

    p_run = sub.add_parser("run", help="документ -> запись в СРО")
    p_run.add_argument("file")
    p_run.add_argument("--dpi", type=int, default=300)
    p_run.add_argument("--live", action="store_true", help="реально сохранить карточку")
    p_run.add_argument("--force", action="store_true", help="создавать даже при найденных дублях")
    p_run.add_argument("--yes", action="store_true", help="без подтверждения в консоли")
    p_run.add_argument("--preview", action="store_true",
                       help="остановиться на заполненной форме и спросить, что с ней делать")
    p_run.add_argument("--profile", default="court_ruling", help="профиль извлечения")
    p_run.add_argument("--scenario", default=DEFAULT_SCENARIO, help="какой сценарий запускать")
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
