"""Список реестра: открыть, найти, проверить дубли, нажать «Добавить».

Один класс обслуживает любой реестр платформы — отличаются только путь и
селектор ссылок в результатах.
"""

from __future__ import annotations

from dataclasses import dataclass

from .client import SroClient


@dataclass
class SearchHit:
    name: str
    card_url: str = ""


class Registry:
    def __init__(self, client: SroClient, path: str, link_key: str = "result_links"):
        self.client = client
        self.path = path
        self.link_key = link_key

    def open(self) -> None:
        self.client.goto(self.path)
        self.client.el("search_input").wait_for(
            state="visible", timeout=self.client.cfg.timeout_ms
        )

    def search(self, query: str) -> list[SearchHit]:
        box = self.client.el("search_input")
        box.fill(query)
        box.press("Enter")
        self.client.settle(0.8)

        links = self.client.page.locator(self.client.sel(self.link_key))
        hits: list[SearchHit] = []
        for i in range(links.count()):
            link = links.nth(i)
            name = " ".join((link.inner_text() or "").split())
            if name:
                hits.append(SearchHit(name=name, card_url=link.get_attribute("href") or ""))
        return hits

    def find_duplicates(self, *queries: str) -> list[SearchHit]:
        """Запросы пробуются по порядку: сначала точный (ИНН), потом ФИО."""
        for query in queries:
            if not query:
                continue
            hits = self.search(query)
            if hits:
                return hits
        return []

    def click_add(self) -> None:
        self.client.require_registry_mutations("Add")
        self.client.click_text("Добавить")
