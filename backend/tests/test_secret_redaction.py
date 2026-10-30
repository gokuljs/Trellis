import pytest

from app.application.secret_redaction import SecretRedactor


@pytest.mark.parametrize(
    "content,secrets,expected",
    [
        ("Ordinary\nsource lines.", ("private-key",), "Ordinary\nsource lines."),
        (
            "First line.\nprivate-\nkey\nLast line.",
            ("private-key",),
            "First line.\n[REDACTED]\nLast line.",
        ),
        (
            "abc\ndefghi\njkl",
            ("abcdef", "defghij"),
            "[REDACTED]kl",
        ),
        (
            "private-\nkey private-key",
            ("private-key",),
            "[REDACTED] [REDACTED]",
        ),
        (
            "Bearer abcdef123\nTOKEN=abcdef123\nsk-fixtureabcdefgh",
            (),
            "Bearer [REDACTED]\nTOKEN=[REDACTED]\n[REDACTED]",
        ),
    ],
)
def test_source_redaction_handles_wrapped_overlapping_and_ordinary_credentials(
    content, secrets, expected
):
    assert SecretRedactor(*secrets).redact_source_text(content) == expected
