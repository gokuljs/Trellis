import pytest

from app.application.errors import ProviderError
from app.application.streaming import TextStreamBatcher


def collect(parts: list[str], *, secrets: tuple[str, ...] = ()) -> str:
    batcher = TextStreamBatcher(secrets=secrets)
    published: list[str] = []
    for index, part in enumerate(parts):
        published.extend(batcher.push(part, now=float(index)))
        published.extend(batcher.flush())
    published.extend(batcher.finish())
    return "".join(published)


def test_safe_text_can_flush_before_completion_without_changing_whitespace() -> None:
    batcher = TextStreamBatcher()
    assert list(batcher.push("  Hello\nworld.  ", now=10.0)) == []
    assert not batcher.flush_due(now=10.099)
    assert batcher.flush_due(now=10.1)
    assert list(batcher.flush()) == ["  Hello\nworld.  "]
    assert list(batcher.finish()) == []


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("before sk-abcdefghijklm after", "before [REDACTED] after"),
        ("before ghp_123456789012 after", "before [REDACTED] after"),
        ("before github_pat_123456789012 after", "before [REDACTED] after"),
        ("Authorization: Bearer abc123.xyz tail", "Authorization: Bearer [REDACTED] tail"),
        ('MY_API_KEY = "abc123def"; tail', 'MY_API_KEY = "[REDACTED]"; tail'),
        ("password='some-password'; tail", "password='[REDACTED]'; tail"),
        ("Bearer some-password'; tail", "Bearer [REDACTED]'; tail"),
        ("sk-proj-password'; tail", "[REDACTED]'; tail"),
    ],
)
def test_credentials_are_redacted_at_every_possible_chunk_boundary(
    content: str, expected: str
) -> None:
    for split in range(len(content) + 1):
        assert collect([content[:split], content[split:]]) == expected


def test_known_secret_with_spaces_is_redacted_across_one_character_chunks() -> None:
    content = "  hello TOP SECRET VALUE! world\n"
    assert collect(list(content), secrets=("TOP SECRET VALUE!",)) == ("  hello [REDACTED] world\n")


def test_overlapping_known_secrets_do_not_release_a_prefix() -> None:
    content = "begin abcdefghi end"
    assert collect(list(content), secrets=("abcdef", "cdefghi")) == "begin ab[REDACTED] end"


def test_late_suffix_cannot_turn_already_published_text_into_a_credential() -> None:
    batcher = TextStreamBatcher()
    assert list(batcher.push("safe sk-short", now=0.0)) == []
    assert list(batcher.flush()) == ["safe "]
    assert list(batcher.push("123456789 tail ", now=1.0)) == []
    assert "".join([*batcher.flush(), *batcher.finish()]) == "[REDACTED] tail "


@pytest.mark.parametrize("tail", ["Bearer abc", "sk-abc", "ghp_123", "github_pat_123"])
def test_terminal_flush_suppresses_unfinished_credential_tokens(tail: str) -> None:
    expected = "Bearer [REDACTED]" if tail.startswith("Bearer") else "[REDACTED]"
    assert collect(["answer ", tail]) == f"answer {expected}"


def test_terminal_flush_suppresses_an_unfinished_known_secret() -> None:
    assert collect(["answer ", "env-key-"], secrets=("env-key-complete",)) == ("answer [REDACTED]")


def test_completed_ordinary_word_does_not_redact_one_letter_secret_prefix() -> None:
    assert collect(["Streaming works"], secrets=("sk-server-test",)) == "Streaming works"


def test_large_input_chunks_produce_bounded_utf8_batches() -> None:
    content = "hello 😀\n" * 30_000
    batcher = TextStreamBatcher()
    published: list[str] = []
    for batch in batcher.push(content, now=0.0):
        assert 0 < len(batch.encode("utf-8")) <= 4096
        assert batcher.pending_bytes <= 8192 + 4096
        published.append(batch)
    published.extend(batcher.finish())
    assert all(len(batch.encode("utf-8")) <= 4096 for batch in published)
    assert "".join(published) == content


def test_small_batches_do_not_split_unicode_characters() -> None:
    batcher = TextStreamBatcher(max_batch_bytes=4)
    published = [*batcher.push("😀 x 😀\n", now=0.0), *batcher.finish()]
    assert all(len(batch.encode("utf-8")) <= 4 for batch in published)
    assert "".join(published) == "😀 x 😀\n"


def test_unicode_known_secret_is_redacted_across_utf8_batch_boundaries() -> None:
    assert collect(list("hello 🔐秘密 😀 bye\n"), secrets=("🔐秘密 😀",)) == (
        "hello [REDACTED] bye\n"
    )


@pytest.mark.parametrize("secret", ["abcabc", "Bearer abc123"])
def test_terminal_flush_redacts_a_complete_literal_with_an_ambiguous_suffix(secret: str) -> None:
    assert collect(list(secret), secrets=(secret,)) == "[REDACTED]"


def test_timer_deadline_tracks_the_oldest_unpublished_checked_text() -> None:
    batcher = TextStreamBatcher()
    assert list(batcher.push("first ", now=2.0)) == []
    assert batcher.next_flush_at == 2.1
    assert list(batcher.push("second ", now=2.05)) == []
    assert batcher.next_flush_at == 2.1
    assert list(batcher.push("", now=2.1)) == ["first second "]
    assert batcher.next_flush_at is None
    assert list(batcher.push("third ", now=3.0)) == []
    assert batcher.next_flush_at == 3.1
    assert list(batcher.finish()) == ["third "]


def test_nonfinite_stream_clock_is_rejected_before_accepting_text() -> None:
    batcher = TextStreamBatcher()
    with pytest.raises(ValueError, match="clock"):
        list(batcher.push("unsafe", now=float("nan")))
    assert list(batcher.finish()) == []


def test_oversized_undecided_spans_fail_closed_and_allow_safe_terminal_flush() -> None:
    batcher = TextStreamBatcher(max_holdback_bytes=64)
    assert list(batcher.push("safe prefix ", now=0.0)) == []
    with pytest.raises(ProviderError, match="streaming safety limit") as error:
        list(batcher.push("Bearer " + "x" * 100_000, now=0.0))
    assert error.value.code == "stream_check_limit"
    assert "Bearer" not in str(error.value)
    assert batcher.pending_bytes <= 64 + 4096
    assert "".join(batcher.finish()) == "safe prefix [REDACTED]"
    with pytest.raises(ValueError, match="closed"):
        list(batcher.push("more", now=1.0))


def test_oversized_holdback_is_cleared_before_a_checked_batch_is_released() -> None:
    batcher = TextStreamBatcher(max_batch_bytes=4, max_holdback_bytes=4)
    iterator = batcher.push("safe " + "x" * 1000, now=0.0)
    assert next(iterator) == "safe"
    assert batcher.pending_bytes <= 8
    with pytest.raises(ProviderError):
        list(iterator)
    terminal = list(batcher.finish())
    assert all(len(batch.encode("utf-8")) <= 4 for batch in terminal)
    assert "".join(terminal) == " [REDACTED]"


def test_whitespace_only_text_is_released_without_final_trimming() -> None:
    assert collect([" \n", "\t\n "]) == " \n\t\n "


def test_known_secret_larger_than_holdback_is_rejected_before_publication() -> None:
    with pytest.raises(ValueError, match="holdback"):
        TextStreamBatcher(secrets=("x" * 65,), max_holdback_bytes=64)


@pytest.mark.parametrize(
    ("batch_bytes", "holdback_bytes", "interval"),
    [
        (3, 8192, 0.1),
        (4097, 8192, 0.1),
        (4096, 0, 0.1),
        (4096, 8192, 0.0),
        (4096, 8192, float("nan")),
    ],
)
def test_invalid_bounds_are_rejected(
    batch_bytes: int, holdback_bytes: int, interval: float
) -> None:
    with pytest.raises(ValueError):
        TextStreamBatcher(
            max_batch_bytes=batch_bytes,
            max_holdback_bytes=holdback_bytes,
            flush_interval_seconds=interval,
        )


def test_completed_stream_rejects_more_text() -> None:
    batcher = TextStreamBatcher()
    assert list(batcher.push("done", now=0.0)) == []
    assert list(batcher.finish()) == ["done"]
    with pytest.raises(ValueError, match="closed"):
        list(batcher.push("extra", now=1.0))
