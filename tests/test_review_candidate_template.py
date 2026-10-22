from app.web.templates import templates
import unittest


class ReviewCandidateTemplateTests(unittest.TestCase):
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
