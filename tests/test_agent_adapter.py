import unittest

from fakes import QUESTION_FORM
from jarvis.agent_adapter import form_answer, is_approval, question_fields


class TestIsApproval(unittest.TestCase):
    def test_refusals_win_over_approval_words(self) -> None:
        for text in ("I'm not sure", "don't allow it", "no, stop", "wait, do not do it", "yes but not now"):
            self.assertFalse(is_approval(text), text)

    def test_clear_approvals(self) -> None:
        for text in ("yes", "go ahead", "Sure, approve it", "okay do it"):
            self.assertTrue(is_approval(text), text)

    def test_words_inside_other_words_do_not_count(self) -> None:
        self.assertFalse(is_approval("I saw that yesterday"))


class TestFormAnswer(unittest.TestCase):
    def test_labels_with_punctuation_match(self) -> None:
        fields = [{"key": "q", "options": [{"value": "cpp", "label": "C++"}, {"value": "env", "label": ".env"}]}]
        self.assertEqual(form_answer(fields, "let's go with C++"), {"q": "cpp"})
        self.assertEqual(form_answer(fields, "put it in .env please"), {"q": "env"})

    def test_named_option_wins(self) -> None:
        self.assertEqual(form_answer(QUESTION_FORM["fields"], "let's do the docs first"), {"q0": "Docs"})

    def test_free_text_when_no_option_named(self) -> None:
        self.assertEqual(form_answer(QUESTION_FORM["fields"], "skip both for now"), {"q0": "skip both for now"})

    def test_boolean_and_multiselect(self) -> None:
        fields = [
            {"key": "ok", "type": "boolean"},
            {
                "key": "pick", "type": "multiselect",
                "options": [{"value": "a", "label": "lint"}, {"value": "b", "label": "tests"}],
            },
        ]
        self.assertEqual(form_answer(fields, "yes, run lint and tests"), {"ok": True, "pick": ["a", "b"]})

    def test_recommended_suffix_is_optional_when_matching(self) -> None:
        fields = [{
            "key": "db",
            "options": [{"value": "pg", "label": "Postgres (Recommended)"}, {"value": "sq", "label": "SQLite (recommended)"}],
        }]
        self.assertEqual(form_answer(fields, "use postgres"), {"db": "pg"})
        self.assertEqual(form_answer(fields, "SQLite"), {"db": "sq"})
        self.assertEqual(form_answer(fields, "Postgres (Recommended)"), {"db": "pg"})

    def test_negated_options_are_excluded(self) -> None:
        fields = [{"key": "db", "options": [{"value": "Postgres", "label": "Postgres"}, {"value": "SQLite", "label": "SQLite"}]}]
        self.assertEqual(form_answer(fields, "not Postgres, use SQLite"), {"db": "SQLite"})
        self.assertEqual(form_answer(fields, "no Postgres, SQLite please"), {"db": "SQLite"})
        self.assertEqual(form_answer(fields, "don't Postgres, use SQLite"), {"db": "SQLite"})
        self.assertEqual(form_answer(fields, "dont Postgres"), {"db": "dont Postgres"})

    def test_question_fields_unifies_claude_and_t3_shapes(self) -> None:
        fields = question_fields([
            {"question": "Pick one", "multiSelect": True, "options": [{"label": "A"}, {"label": "B", "value": "b"}]}
        ])
        self.assertEqual(fields, [{
            "key": "Pick one",
            "description": "Pick one",
            "type": "multiselect",
            "options": [{"value": "A", "label": "A"}, {"value": "b", "label": "B"}],
        }])


if __name__ == "__main__":
    unittest.main()
