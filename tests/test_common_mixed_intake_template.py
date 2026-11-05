from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from jinja2 import DictLoader, Environment


TEMPLATES = Path(__file__).parents[1] / "app" / "web" / "templates"


def _url_for(endpoint: str, **values: object) -> str:
    return _URL("/" + endpoint.replace("_", "-") + ("/" + "/".join(str(value) for value in values.values()) if values else ""))


class _URL(str):
    def include_query_params(self, **values: object) -> "_URL":
        return _URL(f"{self}?{urlencode(values)}")


def test_mixed_registry_entry_renders_upload_only_page():
    template = Environment(loader=DictLoader({
        "base.html": "{% block title %}{% endblock %}{% block extra_head %}{% endblock %}{% block content %}{% endblock %}",
        "tasks.html": (TEMPLATES / "tasks.html").read_text(),
    })).get_template("tasks.html")
    rendered = template.render(
        request=SimpleNamespace(query_params={"registry": "mixed"}),
        url_for=_url_for,
        upload_idempotency_key="0" * 48,
        upload_max_files=20,
        upload_max_batch_files=100,
        upload_max_file_bytes=10_000,
        upload_max_batch_bytes=100_000,
    )
    assert "Загрузить смешанную партию" in rendered
    assert "data-mixed-batch=\"true\"" in rendered
    assert "Общая загрузка" in rendered
    assert "Загрузить и подготовить" in rendered
    assert "создаст пустую техническую карточку СРО" in rendered
    assert "нажмёт отдельно после проверки" in rendered
    assert "Найти судебное дело" not in rendered
    assert "Обработка документов" not in rendered


def test_primary_navigation_exposes_common_upload_entry():
    template = Environment().from_string((TEMPLATES / "base.html").read_text())
    rendered = template.render(
        request=SimpleNamespace(
            query_params={"registry": "mixed"},
            url=SimpleNamespace(path="/debtors/intake"),
        ),
        user=SimpleNamespace(full_name="Оператор", login="operator"),
        review_asset_versions={"staff-controls.js": "1", "app.css": "1"},
        url_for=_url_for,
    )
    assert 'href="/debtors/intake?registry=mixed#document-upload"' in rendered
    assert "Общая загрузка" in rendered
    assert 'aria-current="page"' in rendered


def test_batch_work_exposes_exact_same_batch_registry_folders():
    template = Environment(loader=DictLoader({
        "base.html": "{% block title %}{% endblock %}{% block content %}{% endblock %}",
        "upload_batch_work.html": (TEMPLATES / "upload_batch_work.html").read_text(),
    })).get_template("upload_batch_work.html")
    batch_id = "a" * 32
    operation = SimpleNamespace(
        index=0, accepted_at=datetime(2026, 11, 5, 10, 13, tzinfo=timezone.utc), accepted_by="Оператор", files=["sample.pdf"],
        operation_id=612, tasks_url="/debtors/intake?intake=612", parent_tasks_url=None,
        parent_operation_id=None,
    )
    context = dict(
        url_for=_url_for,
        batch=SimpleNamespace(id=batch_id, chunk_count=1),
        parent_batch=None, child_batches=[], operations=[operation], missing_chunks=[],
        mixed={"batch_id": batch_id},
        mixed_all_counts={"persons": 2, "organizations": 1, "assignments": 1},
        selected_registry=None,
        mixed_api_url=f"/upload-batches/{batch_id}/mixed",
        mixed_prepare_url=f"/upload-batches/{batch_id}/mixed/prepare",
        mixed_save_url=f"/upload-batches/{batch_id}/mixed/save",
        mixed_reconcile_url=f"/upload-batches/{batch_id}/mixed/reconcile-local",
    )
    rendered = template.render(**context)
    assert f"/upload-batch-work-page/{batch_id}?registry=persons" in rendered
    assert f"/upload-batch-work-page/{batch_id}?registry=organizations" in rendered
    assert f"/upload-batch-work-page/{batch_id}?registry=assignments" in rendered
    assert "Физические лица · 2" in rendered
    assert "Назначения · 1" in rendered
    assert "sample.pdf" in rendered
    assert "Дополнительно: квитанции загрузки и транспорт" in rendered
    assert "05.11.2026 10:13" in rendered
    assert "2026-11-05T" not in rendered
    assert "при Add" in rendered
    assert "заполнит эту же карточку" in rendered

    filtered = template.render(**{**context, "selected_registry": "persons"})
    assert "Папка «Физические лица»" in filtered
    assert "К полной партии и квитанциям загрузки" in filtered
    assert "sample.pdf" not in filtered


def test_common_upload_sidebar_is_active_on_batch_work_page():
    template = Environment().from_string((TEMPLATES / "base.html").read_text())
    rendered = template.render(
        request=SimpleNamespace(
            query_params={},
            url=SimpleNamespace(path="/upload-batches/" + "a" * 32 + "/work"),
        ),
        user=SimpleNamespace(full_name="Оператор", login="operator"),
        review_asset_versions={"staff-controls.js": "1", "app.css": "1"},
        url_for=_url_for,
    )
    assert 'href="/debtors/intake?registry=mixed#document-upload" class="app-nav-link is-current current"' in rendered
