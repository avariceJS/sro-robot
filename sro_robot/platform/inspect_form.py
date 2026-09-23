"""Снимает реальную структуру формы СРО, чтобы зафиксировать селекторы.

Запускается один раз (и после каждого обновления платформы вендором):
проходит по шагам ручного сценария и пишет карту полей в out/.
"""

from __future__ import annotations

import json

from ..config import Config
from .client import SroClient
from .pages import PERSON_LIST_PATH
from .probe import visible_buttons, visible_inputs
from .registry import Registry


def run(
    cfg: Config | None = None,
    list_path: str = PERSON_LIST_PATH,
    out_name: str = "form_map.json",
    wait_for_manual_login: bool = True,
) -> dict:
    cfg = cfg or Config.load()
    cfg.headless = False  # инспектору нужен видимый браузер
    result: dict = {}

    with SroClient(cfg, dry_run=True) as client:
        page = client.page
        page.goto(cfg.base_url, wait_until="domcontentloaded")
        client.settle()

        if not client.is_logged_in():
            result["login_page_fields"] = list(visible_inputs(page))
            if cfg.login and cfg.password:
                client.ensure_logged_in()
            elif wait_for_manual_login:
                print("Войдите в систему в открытом окне браузера и нажмите Enter здесь...")
                input()
                client.settle()

        client.shot("inspect_home")
        result["home_menu"] = page.evaluate(
            """() => Array.from(document.querySelectorAll('a'))
                    .map(a => ({text: a.innerText.trim(), href: a.getAttribute('href') || ''}))
                    .filter(a => a.text)"""
        )

        registry = Registry(client, list_path)
        registry.open()
        result["registry_fields"] = list(visible_inputs(page))

        registry.click_add()
        client.settle(0.8)
        client.shot("inspect_new_card")
        result["card_general_fields"] = list(visible_inputs(page))
        result["card_buttons"] = list(visible_buttons(page))
        result["card_tabs"] = page.evaluate(
            """() => Array.from(document.querySelectorAll('a, li, span'))
                    .map(e => e.innerText.trim())
                    .filter(t => t && t.length < 30 &&
                        /Общая информация|Контакты|ГосРегистрация|Статус|Файлы/.test(t))"""
        )

        try:
            client.click_text("Контакты")
            client.settle(0.6)
            client.shot("inspect_contacts")
            result["contacts_fields"] = list(visible_inputs(page))

            client.click_text("Добавить адрес")
            client.settle(0.8)
            client.shot("inspect_address_modal")
            result["address_modal_fields"] = list(visible_inputs(page))
            result["address_modal_buttons"] = list(visible_buttons(page))
        except Exception as exc:
            result["contacts_error"] = str(exc)

        out = cfg.out_dir / out_name
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Карта формы сохранена: {out}")

    return result
