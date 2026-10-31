import json
import unittest
from uuid import uuid4

from app.application.research_claim_binding import bind_emphasized_claim
from app.application.research_reports import parse_report
from app.domain.research import RESEARCH_POLICY_V2


class ResearchClaimBindingTests(unittest.TestCase):
    def test_bold_role_and_bare_application_url_bind_to_exact_markdown_span(self):
        plain = "Airbnb lists a Staff AI Engineer role in San Francisco. Apply: https://careers.airbnb.com/positions/123"
        raw = "Airbnb lists a **Staff AI Engineer** role in San Francisco. Apply: https://careers.airbnb.com/positions/123"
        self.assertEqual(bind_emphasized_claim(raw + " [[source:reference]]", plain), raw)

    def test_company_names_and_nested_emphasis_preserve_original_characters(self):
        for raw, plain in (
            (
                "**Snorkel AI** lists an **AI Engineer** role.",
                "Snorkel AI lists an AI Engineer role.",
            ),
            ("A ***senior engineer*** role.", "A senior engineer role."),
            ("__A__ has an _engineer_ role.", "A has an engineer role."),
            ("A **senior\nengineer** role.", "A senior\nengineer role."),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(bind_emphasized_claim(raw, plain), raw)

    def test_binding_does_not_change_claim_words_or_interpret_other_markdown(self):
        for raw, plain in (
            ("**Airbnb** does not offer remote work.", "Airbnb offers remote work."),
            ("**Airbnb** pays $120,000.", "Airbnb pays $125,000."),
            ("**Airbnb** lists an engineer role.", "Airbnb lists an engineer role!"),
            ("**Airbnb** lists  an engineer role.", "Airbnb lists an engineer role."),
            ("**foo_bar** hires engineers.", "foobar hires engineers."),
            ("**Airbnb** <b>hires</b> engineers.", "Airbnb hires engineers."),
            (
                "[**Airbnb**](https://wrong.example/jobs) hires engineers.",
                "Airbnb hires engineers.",
            ),
            (
                "**Airbnb** ![hires](https://example.com/image) engineers.",
                "Airbnb hires engineers.",
            ),
            ("**Airbnb** &amp; Snorkel hire engineers.", "Airbnb & Snorkel hire engineers."),
            (r"**Airbnb** hires \*engineers\*.", "Airbnb hires *engineers*."),
            ("**Airbnb** hires `engineers`.", "Airbnb hires engineers."),
            ("**Airbnb** hires **engineers.", "Airbnb hires engineers."),
            (
                "**Airbnb** hires engineers.\n\nAirbnb hires **engineers**.",
                "Airbnb hires engineers.",
            ),
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(bind_emphasized_claim(raw, plain))

    def test_only_v2_report_parsing_admits_exact_emphasis_span(self):
        source = str(uuid4())
        plain = "Snorkel AI lists an AI Engineer role."
        raw = "**Snorkel AI** lists an **AI Engineer** role."
        value = {
            "markdown": (
                f"## Summary\n\n{raw} [[source:{source}:L1-L1]]\n\n"
                "## Findings\n\nSee summary.\n\n## Conflicts\n\nNone.\n\n"
                "## Limitations\n\nCurrent listing only."
            ),
            "claims": [
                {
                    "id": "summary",
                    "text": plain,
                    "citations": [{"source_id": source, "start_line": 1, "end_line": 1}],
                }
            ],
            "covered_question_ids": [],
            "stop_reason": "collection_complete",
        }
        with self.assertRaisesRegex(ValueError, "research claim is absent"):
            parse_report(json.dumps(value))
        report = parse_report(json.dumps(value), policy_version=RESEARCH_POLICY_V2)
        self.assertEqual(report.claims[0].text, raw)
        self.assertIn(report.claims[0].text, report.markdown)


if __name__ == "__main__":
    unittest.main()
