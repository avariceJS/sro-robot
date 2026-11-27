from pathlib import Path
from types import SimpleNamespace
import unittest

from jinja2 import DictLoader, Environment

from app.db.models import TaskStatus
from app.web.templates import templates


class ReviewCandidateTemplateTests(unittest.TestCase):
    def test_inline_field_warning_uses_fixed_copy_and_links_to_input(self) -> None:
        template_text = (Path(__file__).parents[1] / "app/web/templates/review.html").read_text()
        template = Environment(loader=DictLoader({
            "base.html": "{% block content %}{% endblock %}",
            "review.html": template_text,
        }), autoescape=True).get_template("review.html")

        class Url(str):
            def include_query_params(self, **_params):
                return self

        fields = [
            dict(attr="doc_date", title="Дата", value="07.07.2099", required=True,
                 warning="Распознавание даты расходится при другом масштабе; проверьте заголовок"),
            dict(attr="inn", title="ИНН", value="", required=False,
                 warning="PRIVATE_OCR_WARNING 500100001146 <script>"),
            dict(attr="court", title="Суд", value="", required=False, warning="поле не найдено"),
        ]
        rendered = template.render(
            task=SimpleNamespace(id=1, status=TaskStatus.approved, scenario="person_card", document=None),
            fields=fields, TaskStatus=TaskStatus, request=SimpleNamespace(query_params={}),
            url_for=lambda *_args, **_kwargs: Url("/synthetic"),
            ocr_retry_context=SimpleNamespace(incoming_run=None, outgoing_run=None),
        )

        self.assertIn('aria-describedby="review-warning-doc_date"', rendered)
        self.assertIn('id="review-warning-doc_date" class="field-warning" role="note"', rendered)
        self.assertIn("Распознавание даты расходится при другом масштабе. Проверьте заголовок.", rendered)
        self.assertIn('aria-describedby="review-warning-inn"', rendered)
        self.assertIn("Проверьте реквизит в оригинале.", rendered)
        self.assertNotIn("PRIVATE_OCR_WARNING", rendered)
        self.assertNotIn("500100001146", rendered)
        self.assertNotIn("review-warning-court", rendered)

    def test_review_only_suggestions_are_escaped_and_birth_details_cannot_transfer(self) -> None:
        template = templates.env.get_template("review_suggestions.html")
        rendered = template.render(
            review_editable=True,
            url_for=lambda endpoint, **kwargs: "/static/review-suggestions.js",
            review_suggestions=[{
                "warning": "Проверьте <script>alert(1)</script>",
                "copyable_fields": ["inn"],
                "fields": {
                    "inn": {
                        "value": '"><img src=x onerror=alert(1)>',
                        "source_variant": 0,
                        "source_start": 5,
                        "source_end": 17,
                        "source_excerpt": "Фрагмент <img src=x onerror=alert(1)>",
                    },
                    "birth_place": {
                        "value": "Макетный город",
                        "source_variant": 0,
                        "source_start": 20,
                        "source_end": 35,
                        "source_excerpt": "место рождения: Макетный город",
                    },
                    "address": {
                        "value": "Фрагмент без номера дома",
                        "source_variant": 0,
                        "source_start": 36,
                        "source_end": 61,
                        "source_excerpt": "адрес регистрации: Фрагмент без номера дома",
                    },
                },
            }],
        )

        self.assertNotIn("<script>alert(1)</script>", rendered)
        self.assertNotIn("<img src=x onerror=alert(1)>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("&lt;img", rendered)
        self.assertIn("data-review-suggestion-copy=\"inn\"", rendered)
        self.assertNotIn("data-review-suggestion-copy=\"address\"", rendered)
        self.assertNotIn("data-review-suggestion-copy=\"birth_place\"", rendered)


if __name__ == "__main__":
    unittest.main()
