"""Снимки структуры страницы для инспектора формы."""

from __future__ import annotations

from typing import Iterable

from playwright.sync_api import Page


def visible_inputs(page: Page) -> Iterable[dict]:
    """Снимок полей страницы для инспектора формы."""
    return page.evaluate(
        """() => {
            const out = [];
            for (const el of document.querySelectorAll('input, select, textarea')) {
                if (el.type === 'hidden') continue;
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) continue;
                let label = '';
                if (el.labels && el.labels.length) label = el.labels[0].innerText;
                if (!label) {
                    const cell = el.closest('td');
                    const prev = cell ? cell.previousElementSibling : null;
                    if (prev) label = prev.innerText;
                }
                if (!label && el.placeholder) label = el.placeholder;
                out.push({
                    tag: el.tagName.toLowerCase(),
                    type: el.type || '',
                    id: el.id || '',
                    name: el.name || '',
                    value: (el.value || '').slice(0, 40),
                    label: (label || '').trim().slice(0, 60),
                    options: el.tagName === 'SELECT'
                        ? Array.from(el.options).map(o => o.text.trim()).slice(0, 40)
                        : undefined,
                });
            }
            return out;
        }"""
    )


def visible_buttons(page: Page) -> Iterable[dict]:
    """Кнопки и ссылки-действия: нужны, чтобы найти правильное «Сохранить»."""
    return page.evaluate(
        """() => {
            const out = [];
            const nodes = document.querySelectorAll(
                "a, button, input[type=submit], input[type=button]");
            for (const el of nodes) {
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) continue;
                const text = (el.innerText || el.value || '').trim();
                if (!text || text.length > 40) continue;
                out.push({
                    tag: el.tagName.toLowerCase(),
                    text: text,
                    id: el.id || '',
                    href: el.getAttribute('href') || '',
                    onclick: (el.getAttribute('onclick') || '').slice(0, 120),
                });
            }
            return out;
        }"""
    )
