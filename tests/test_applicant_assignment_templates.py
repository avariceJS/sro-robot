from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from jinja2 import DictLoader, Environment


TEMPLATES = Path(__file__).parents[1] / "app" / "web" / "templates"


def _url_for(endpoint: str, **values: object) -> str:
    suffix = "/".join(str(value) for value in values.values())
    return f"/{endpoint}" + (f"/{suffix}" if suffix else "")


def _environment() -> Environment:
    return Environment(loader=DictLoader({"base.html": "{% block title %}{% endblock %}{% block content %}{% endblock %}"}))


def _draft() -> SimpleNamespace:
    return SimpleNamespace(
        id=91,
        status="draft",
        source_role="applicant",
        source_task_id=77,
        debtor_card_id=None,
        debtor_card=None,
        case_number="А40-123/2026",
        values={"applicant": "Иванов Иван", "practitioner": ""},
        version=1,
        updated_at=datetime(2026, 11, 5, 12, 30),
        updated_by=SimpleNamespace(full_name="Оператор", login="operator"),
    )


def test_applicant_assignment_list_shows_applicant_and_missing_debtor_and_manager():
    template = Environment(loader=DictLoader({
        "base.html": "{% block title %}{% endblock %}{% block content %}{% endblock %}",
        "assignments.html": (TEMPLATES / "assignments.html").read_text(),
    })).get_template("assignments.html")
    rendered = template.render(
        url_for=_url_for, drafts=[_draft()], statuses={"draft": "Черновик"},
        total=1, query="", selected_status="", page=1, pages=1,
        previous_url="", next_url="", source_batch_work_urls={91: "/upload-batches/abc/work?registry=assignments"},
    )
    assert "Заявитель: Иванов Иван" in rendered
    assert "Должник не выбран" in rendered
    assert "Ожидает сведения об управляющем" in rendered
    assert 'href="/upload-batches/abc/work?registry=assignments"' in rendered


def test_applicant_assignment_detail_and_revision_render_without_debtor_card():
    environment = _environment()
    detail = environment.from_string((TEMPLATES / "assignment.html").read_text())
    rendered = detail.render(
        url_for=_url_for, draft=_draft(), statuses={"draft": "Черновик"}, entity_kinds={},
        debtor_options_url="/assignments/91/debtor-options",
        bind_debtor_url="/assignments/91/bind-debtor",
        source_batch_work_url="/upload-batches/abc/work?registry=assignments",
        source_documents=[], membership_packages=[], fields=[], revisions=[], revision_total=0,
        revision_page=1, revision_pages=1, revision_previous_url="", revision_next_url="",
        error="", notice="", conflict_rows=[], form_values={}, form_initially_dirty=False,
        validation_message="",
    )
    assert "Заявитель: <strong>Иванов Иван</strong>" in rendered
    assert "Должник: не выбран" in rendered
    assert "Заявитель сохранён как источник назначения" in rendered
    assert "Сведения об арбитражном управляющем пока не указаны" in rendered
    assert 'data-options-url="/assignments/91/debtor-options"' in rendered
    assert 'action="/assignments/91/bind-debtor"' in rendered
    assert 'name="version" value="1"' in rendered
    assert "Открыть папку назначений исходной партии" in rendered

    bound_draft = _draft()
    bound_draft.debtor_card_id = 23
    bound_draft.debtor_card = SimpleNamespace(
        id=23, display_name="Проверенный должник", entity_kind="person", source_task_id=None,
    )
    bound = detail.render(
        url_for=_url_for, draft=bound_draft, statuses={"draft": "Черновик"}, entity_kinds={"person": "Физическое лицо"},
        debtor_options_url="/assignments/91/debtor-options", bind_debtor_url="/assignments/91/bind-debtor",
        source_batch_work_url="/upload-batches/abc/work?registry=assignments",
        source_documents=[], membership_packages=[], fields=[], revisions=[], revision_total=0,
        revision_page=1, revision_pages=1, revision_previous_url="", revision_next_url="",
        error="", notice="", conflict_rows=[], form_values={}, form_initially_dirty=False,
        validation_message="",
    )
    assert "Проверенный должник" in bound
    assert "Заявитель: <strong>Иванов Иван</strong>" in bound
    assert "applicant-debtor-picker" not in bound

    revision = environment.from_string((TEMPLATES / "assignment_revision.html").read_text())
    historical = revision.render(
        url_for=_url_for, draft=_draft(), revision=SimpleNamespace(
            revision=1, created_at=datetime(2026, 11, 5, 12, 30),
        ), actor_name="Оператор", initial=True, previous_revision=None, changes=[],
    )
    assert "Заявитель: Иванов Иван" in historical
    assert "должник не выбран" in historical
