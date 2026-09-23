# Архитектура

## Структура (разделение сделано 22.09.2026)

Код разделён по слоям, чтобы новые задачи добавлялись файлом, а не правкой
работающего. Решение принято 20.09.2026, см. `70-decisions.md`.

```
sro_robot/
  platform/      # умение работать с платформой: вход, settle, поиск, кнопки, скриншоты
  scenarios/     # инструкции: person_card, дальше firm_card, assignment
  documents/     # профили извлечения по типу документа (определение, заявление, решение)
  core/          # ocr, extract, morph, address, validators, models
  cli.py, config.py
```

Зависимости идут в одну сторону: `cli` → `scenarios` → `platform` + `core`.
`core` не знает о браузере, `platform` не знает о документах.

## Контракт сценария (`scenarios/base.py`)

Одинаков для всех сценариев и не зависит от интерфейса — консоль и будущий веб
вызывают одно и то же:

| Что | Метод | Результат |
| --- | --- | --- |
| какие поля нужны на входе | `scenario.fields` | кортеж `FieldSpec(attr, title, required)` |
| проверка данных без сети | `scenario.validate(card)` | названия незаполненных обязательных полей |
| вход, реестр, поиск дублей | `scenario.preflight(client, card)` | `Preflight(missing, duplicates)` |
| запуск | `scenario.run(client, card, gate=None)` | `ScenarioResult(ok, saved, record_url, filled, screenshots)` |

`gate` — это `ReviewGate`: способ спросить оператора, что делать с заполненной
формой. Без него поведение прежнее (пробный прогон закрывает черновик), с ним
робот останавливается и ждёт ответа `"save"` или `"close"`. Консоль реализует
его через `input()`, веб — через базу и страницу задания.

Решение «создавать ли при найденных дублях» принимает вызывающая сторона:
сценарий только сообщает, что нашёл. У обеих структур есть `to_dict()` —
веб отдаст их как JSON без переделки.

Сценарий выбирается по имени: `get_scenario("person_card")`, реестр имён —
в `scenarios/__init__.py`. Профиль документа так же: `get_profile("court_ruling")`.

## Текущий поток данных

```
файл (jpg/pdf) -> core/ocr.py -> список вариантов текста
                              -> core/extract.py (голосование) -> DebtorCard
                              -> cli.py (проверка оператором)
                              -> scenarios/person_card.py
                                 -> platform/ (Playwright) -> карточка в СРО
```

## Модули

| Файл | Ответственность | Не делает |
| --- | --- | --- |
| `core/ocr.py` | PDF/изображение → **несколько** вариантов текста | не парсит поля |
| `core/extract.py` | текст → `DebtorCard`, голосование между вариантами | не ходит в сеть |
| `core/validators.py` | контрольные суммы ИНН/СНИЛС, номер дела | — |
| `core/morph.py` | падежи ФИО, пол по отчеству | — |
| `core/address.py` | адрес одной строкой → поля модалки | — |
| `core/models.py` | `Field` (значение + точность + предупреждение), `DebtorCard` | не знает об обязательных полях формы |
| `config.py` | `config.yaml` + секреты из `.env` | не хранит пароли в коде |
| `platform/client.py` | браузер, вход, `settle`, `el/fill/select`, «Сохранить», скриншоты | не знает, что за карточка |
| `platform/pages.py` | пути страниц и селекторы (`COMMON_`, `PERSON_SELECTORS`) | — |
| `platform/registry.py` | список реестра: открыть, поиск, дубли, «Добавить» | один класс на все реестры |
| `platform/probe.py` | снимок полей и кнопок страницы | — |
| `platform/inspect_form.py` | карта формы в `out/form_map*.json` | не создаёт карточки |
| `scenarios/base.py` | контракт: `FieldSpec`, `Preflight`, `ScenarioResult` | — |
| `scenarios/person_card.py` | карточка должника-физлица | не парсит документы |
| `documents/` | профиль документа: чем разбирать файл этого типа | пока один профиль |
| `cli.py` | команды `extract`, `inspect`, `run` | не содержит логики парсинга и заполнения |

## Ключевой тип

Каждое поле — это `Field(value, confidence, source, warning)`, а не строка.
Точность и предупреждение нужны, чтобы оператор знал, что перепроверить.
При добавлении поля сохраняй этот контракт, иначе сломается вывод в `cli.py`.

## Команды

```bash
.venv/bin/python -m sro_robot extract samples/opredelenie_1.jpg   # только распознать
.venv/bin/python -m sro_robot inspect                             # снять карту формы
.venv/bin/python -m sro_robot inspect --registry firm             # то же для юрлиц
.venv/bin/python -m sro_robot run samples/opredelenie_1.jpg        # пробный прогон
.venv/bin/python -m sro_robot run samples/... --live               # с записью в СРО
```

Флаги `run`: `--live` (сохранить), `--force` (создать при найденных дублях),
`--yes` (без подтверждения в консоли), `--dpi` (плотность рендера PDF).

## Внешние зависимости

Системные: `tesseract` с языком `rus`, `poppler` (pdftotext, pdf2image),
Chromium для Playwright. Python: `pytesseract`, `pillow`, `pdf2image`,
`pymorphy3`, `playwright`, `pyyaml`, `rich`. Окружение — `.venv` в корне.
