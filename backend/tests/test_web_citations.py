import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event as ThreadEvent
from unittest.mock import AsyncMock

import pytest
from run_fixtures import Provider, Repository, plan, service
from web_fixtures import SOURCE

from app.application.errors import ApplicationError, ProviderError
from app.application.run_context import RunContext
from app.application.web_citations import MAX_CITATION_ANSWER_BYTES, answer_citations
from app.domain.recovery import (
    RECOVERY_RUNTIME_VERSION,
    RecoveryAction,
    RecoveryCompatibility,
    RecoveryContract,
    RecoverySnapshot,
    resolve_recovery,
)
from app.domain.runtime import ModelCallStatus, ModelStreamEvent, RunStatus
from app.domain.web import CitationReference

MARKER = f"[[source:{SOURCE}:L1-L2]]"


def test_answer_citations_canonicalize_and_deduplicate_in_first_appearance_order():
    other = "32000000-0000-4000-8000-000000000abc"
    content = f"First {MARKER}, again {MARKER}, then [[source:{other.upper()}:L3-L3]]."
    assert answer_citations(content) == (
        CitationReference(SOURCE, 1, 2),
        CitationReference(other, 3, 3),
    )


@pytest.mark.parametrize(
    "content",
    [
        f"`{MARKER}`",
        f"``quote `{MARKER}` ``",
        f"```python\n{MARKER}\n```",
        f"~~~\n{MARKER}\n~~~",
        f"> ```\n> {MARKER}\n> ```",
        "- ```\n  [[source:bad]]\n  ```",
        f"[{MARKER}](https://example.com/{MARKER})",
        f"[{MARKER}][label]\n\n[label]: https://example.com",
        f"[label]: https://example.com/{MARKER}",
        f"![{MARKER}](https://example.com/image.png)",
        f"<https://example.com/{MARKER}>",
        f"https://example.com/{MARKER}",
        f'<span title="{MARKER}">hello</span>',
        f"<!-- {MARKER} -->",
        "<div>\n[[source:bad]]\n</div>",
    ],
)
def test_literal_and_linked_markers_are_not_supporting_citations(content):
    assert answer_citations(content) == ()


@pytest.mark.parametrize(
    "content",
    [
        "[[source:",
        "[[source:unknown:L1-L2]]",
        f"[[source:{SOURCE}:L0-L2]]",
        f"[[source:{SOURCE}:L2-L1]]",
        f"[[source:{SOURCE}:L1-L201]]",
        f"[[source:{SOURCE}:L1-L2]",
        f"[[source:{SOURCE}:L1]]",
        f"[[source:{SOURCE}:L01-L2]]",
        " ".join(f"[[source:{SOURCE}:L{index}-L{index}]]" for index in range(1, 52)),
    ],
)
def test_final_answers_reject_malformed_unfinished_or_unbounded_citations(content):
    with pytest.raises(ProviderError) as failure:
        answer_citations(content)
    assert failure.value.code == "citation_invalid"
    assert "support" in failure.value.message


def test_emphasis_can_surround_a_marker_but_cannot_construct_its_syntax():
    assert answer_citations(f"**{MARKER}**") == (CitationReference(SOURCE, 1, 2),)
    with pytest.raises(ProviderError):
        answer_citations(f"[[source:**{SOURCE}**:L1-L2]]")
    assert answer_citations(f"&#91;&#91;source:{SOURCE}:L1-L2]]") == ()


def test_visible_angle_prose_and_indented_paragraph_continuations_keep_citations():
    assert answer_citations(f"< prose {MARKER} >") == (CitationReference(SOURCE, 1, 2),)
    assert answer_citations(f"Paragraph\n    still prose {MARKER}") == (
        CitationReference(SOURCE, 1, 2),
    )
    assert answer_citations("[" * 100_000) == ()
    assert answer_citations("[" * 60_000 + " " + MARKER) == (CitationReference(SOURCE, 1, 2),)
    assert answer_citations(f"Text <span>{MARKER}</span>") == (CitationReference(SOURCE, 1, 2),)
    assert answer_citations(f"[{MARKER}]\n\n[{MARKER}]: https://example.com") == (
        CitationReference(SOURCE, 1, 2),
    )


@pytest.mark.parametrize(
    "content",
    [
        "[example](bad url [[source:bad]])",
        '[example](destination "unterminated [[source:bad]])',
        "[example](destination\n\n[[source:bad]])",
        "[x]: bad url [[source:bad]]",
        "[label [[source:bad]]][x]\n\n[x]: bad url",
        "https://[[source:bad]]",
        "abchttps://example.com/[[source:bad]]",
        "abcwww.example.com/[[source:bad]]",
        "https://bad_host.com/[[source:bad]]",
        "https://aé.example.c_m/[[source:bad]]",
        "user@example.com/[[source:bad]]",
        "user+[[source:bad]]@example.com",
        "mailto:user@example.com/[[source:bad]]",
    ],
)
def test_visible_malformed_markers_are_not_hidden_by_invalid_links_or_url_text(content):
    with pytest.raises(ProviderError) as failure:
        answer_citations(content)
    assert failure.value.code == "citation_invalid"


@pytest.mark.parametrize(
    "content",
    [
        "```\n    ```\n[[source:bad]]\n```",
        "- ```\n        ```\n  [[source:bad]]\n  ```",
        "> ```\n>     ```\n> [[source:bad]]\n> ```",
        "[label [[source:bad]]](https://example.com)",
        "[label [[source:bad]]](javascript:alert(1))",
        "![alt [[source:bad]]](https://example.com/image.png)",
        '<span title="[[source:bad]]">hello</span>',
        "<div>\n[[source:bad]]\n</div>",
        "www.example.com/[[source:bad]]",
        "www.example.com?q=[[source:bad]]",
        "www.[[source:bad]].com",
        "https://example..com/[[source:bad]]",
        "https://example.com:bad/[[source:bad]]",
        "https://user:pass@example.com/[[source:bad]]",
        "https://999.999.999.999/[[source:bad]]",
    ],
)
def test_malformed_citation_text_inside_native_ignored_markdown_contexts_is_literal(content):
    assert answer_citations(content) == ()


def test_citation_validation_has_a_utf8_byte_bound_and_no_prefix_fastpath():
    boundary = "a" * (MAX_CITATION_ANSWER_BYTES - len(MARKER.encode("utf-8"))) + MARKER
    assert answer_citations(boundary) == (CitationReference(SOURCE, 1, 2),)
    for content in (boundary + "a", "🪴" * (MAX_CITATION_ANSWER_BYTES // 4) + MARKER):
        with pytest.raises(ProviderError) as failure:
            answer_citations(content)
        assert failure.value.code == "citation_validation_limit"
    assert answer_citations("x" * (MAX_CITATION_ANSWER_BYTES * 2)) == ()


class AnswerProvider(Provider):
    def __init__(self, content):
        super().__init__()
        self.content = content
        self.calls = 0

    async def stream(self, *args):
        self.calls += 1
        yield ModelStreamEvent("text_delta", text=self.content)
        yield ModelStreamEvent(
            "usage", input_tokens=100, output_tokens=10, cached_tokens=0, cache_creation_tokens=0
        )
        yield ModelStreamEvent("completed", finish_kind="final")


@pytest.mark.parametrize("failure_code", [None, "citation_invalid", "database_unavailable"])
def test_final_citation_binding_precedes_promotion_and_preserves_paid_usage(
    failure_code, monkeypatch
):
    async def exercise():
        repository = Repository()
        provider = AnswerProvider(f"Supported answer. {MARKER}")
        runtime = service(repository, provider)
        evidence = AsyncMock()
        if failure_code:
            evidence.bind_web_citations.side_effect = ApplicationError(failure_code, "No support")
        runtime._web_repository = evidence
        monkeypatch.setattr(RunContext, "prepare", AsyncMock(return_value=plan()))
        commit = AsyncMock(wraps=repository.commit_generation)
        monkeypatch.setattr(repository, "commit_generation", commit)
        run = await runtime.create_run("session", "request", "hello")
        await runtime.wait_for_run(run.id)
        assert provider.calls == 1
        assert repository.calls[-1].status is ModelCallStatus.COMPLETED
        assert repository.calls[-1].input_tokens == 100
        assert repository.calls[-1].output_tokens == 10
        assert repository.calls[-1].accounted_total_tokens == 110
        evidence.bind_web_citations.assert_awaited_once_with(
            run.id, run.generation, repository.calls[-1].id, (CitationReference(SOURCE, 1, 2),)
        )
        if failure_code:
            commit.assert_not_awaited()
            assert repository.run.status is RunStatus.FAILED
            assert repository.events[-1].data["code"] == failure_code
        else:
            assert commit.await_count == 1
            assert repository.run.status is RunStatus.COMPLETED
        await runtime.close()

    asyncio.run(exercise())


def test_plain_answers_do_not_require_web_storage(monkeypatch):
    async def exercise():
        repository = Repository()
        runtime = service(repository, Provider())
        evidence = AsyncMock()
        runtime._web_repository = evidence
        monkeypatch.setattr(RunContext, "prepare", AsyncMock(return_value=plan()))
        run = await runtime.create_run("session", "request", "hello")
        await runtime.wait_for_run(run.id)
        assert repository.run.status is RunStatus.COMPLETED
        evidence.bind_web_citations.assert_not_awaited()
        await runtime.close()

    asyncio.run(exercise())


def test_citation_parsing_leaves_the_event_loop_responsive(monkeypatch):
    async def exercise():
        entered, released = asyncio.Event(), ThreadEvent()
        loop = asyncio.get_running_loop()

        def parse(content):
            loop.call_soon_threadsafe(entered.set)
            if not released.wait(2):
                raise RuntimeError("Citation parsing blocked the event loop")
            return (CitationReference(SOURCE, 1, 2),)

        async def heartbeat():
            await entered.wait()
            released.set()

        repository = Repository()
        runtime = service(repository, AnswerProvider(MARKER))
        runtime._web_repository = AsyncMock()
        monkeypatch.setattr(RunContext, "prepare", AsyncMock(return_value=plan()))
        monkeypatch.setattr("app.application.runs.answer_citations", parse)
        pulse = asyncio.create_task(heartbeat())
        try:
            run = await runtime.create_run("session", "request", "hello")
            await runtime.wait_for_run(run.id)
            assert repository.run.status is RunStatus.COMPLETED
        finally:
            released.set()
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
            await runtime.close()

    asyncio.run(exercise())


def test_citation_validation_limit_retains_paid_usage_without_binding_or_promotion(monkeypatch):
    async def exercise():
        repository = Repository()
        provider = AnswerProvider("text " * (MAX_CITATION_ANSWER_BYTES // 5 + 1) + MARKER)
        runtime = service(repository, provider)
        evidence = AsyncMock()
        runtime._web_repository = evidence
        monkeypatch.setattr(RunContext, "prepare", AsyncMock(return_value=plan()))
        commit = AsyncMock(wraps=repository.commit_generation)
        monkeypatch.setattr(repository, "commit_generation", commit)
        run = await runtime.create_run("session", "request", "hello")
        await runtime.wait_for_run(run.id)
        assert provider.calls == 1
        assert repository.run.status is RunStatus.FAILED
        assert repository.events[-1].data["code"] == "citation_validation_limit"
        assert repository.calls[-1].status is ModelCallStatus.COMPLETED
        assert repository.calls[-1].accounted_total_tokens == 110
        assert repository.calls[-1].estimated_cost is not None
        evidence.bind_web_citations.assert_not_awaited()
        commit.assert_not_awaited()
        await runtime.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("exception", [TimeoutError(), RuntimeError("Invalid binding receipt")])
def test_binding_failures_retain_paid_completion_without_promoting_or_dispatching_again(
    exception, monkeypatch
):
    async def exercise():
        repository = Repository()
        provider = AnswerProvider(MARKER)
        runtime = service(repository, provider)
        evidence = AsyncMock()
        evidence.bind_web_citations.side_effect = exception
        runtime._web_repository = evidence
        monkeypatch.setattr(RunContext, "prepare", AsyncMock(return_value=plan()))
        commit = AsyncMock(wraps=repository.commit_generation)
        monkeypatch.setattr(repository, "commit_generation", commit)
        run = await runtime.create_run("session", "request", "hello")
        await runtime.wait_for_run(run.id)
        assert repository.run.status is RunStatus.FAILED
        assert repository.calls[-1].status is ModelCallStatus.COMPLETED
        assert repository.calls[-1].accounted_total_tokens == 110
        assert repository.calls[-1].estimated_cost is not None
        assert provider.calls == 1
        commit.assert_not_awaited()
        await runtime.close()

    asyncio.run(exercise())


def test_cancellation_during_binding_preserves_paid_usage_without_promoting(monkeypatch):
    async def exercise():
        repository = Repository()
        provider = AnswerProvider(MARKER)
        runtime = service(repository, provider)
        entered = asyncio.Event()

        async def binding(*args):
            entered.set()
            await asyncio.Event().wait()

        evidence = AsyncMock()
        evidence.bind_web_citations.side_effect = binding
        runtime._web_repository = evidence
        monkeypatch.setattr(RunContext, "prepare", AsyncMock(return_value=plan()))
        commit = AsyncMock(wraps=repository.commit_generation)
        monkeypatch.setattr(repository, "commit_generation", commit)
        run = await runtime.create_run("session", "request", "hello")
        await asyncio.wait_for(entered.wait(), 1)
        repository.run = replace(repository.run, status=RunStatus.CANCELLING)
        runtime._tasks[run.id].cancel()
        await asyncio.gather(runtime.wait_for_run(run.id), return_exceptions=True)
        assert repository.run.status is RunStatus.CANCELLED
        assert repository.calls[-1].status is ModelCallStatus.COMPLETED
        assert repository.calls[-1].accounted_total_tokens == 110
        assert repository.calls[-1].estimated_cost is not None
        assert provider.calls == 1
        commit.assert_not_awaited()
        await runtime.close()

    asyncio.run(exercise())


def test_lost_successful_binding_reply_keeps_orphan_binding_unpromoted(monkeypatch):
    async def exercise():
        repository = Repository()
        provider = AnswerProvider(MARKER)
        runtime = service(repository, provider)
        accepted = []

        async def binding(*args):
            accepted.append(args)
            raise ApplicationError("database_unavailable", "The binding receipt was lost.")

        evidence = AsyncMock()
        evidence.bind_web_citations.side_effect = binding
        runtime._web_repository = evidence
        monkeypatch.setattr(RunContext, "prepare", AsyncMock(return_value=plan()))
        commit = AsyncMock(wraps=repository.commit_generation)
        monkeypatch.setattr(repository, "commit_generation", commit)
        run = await runtime.create_run("session", "request", "hello")
        await runtime.wait_for_run(run.id)
        assert len(accepted) == 1 and provider.calls == 1
        assert repository.run.status is RunStatus.FAILED
        assert repository.calls[-1].accounted_total_tokens == 110
        commit.assert_not_awaited()
        await runtime.close()

    asyncio.run(exercise())


def test_unpromoted_paid_final_answer_cannot_resume_or_dispatch_again():
    from run_fixtures import model_record, run_record

    installation = "32000000-0000-4000-8000-000000000001"
    run = replace(
        run_record(RunStatus.RECOVERING),
        recovery_contract=RecoveryContract(
            1, RECOVERY_RUNTIME_VERSION, (), (), "request", installation, None
        ),
    )
    call = replace(
        model_record(),
        status=ModelCallStatus.COMPLETED,
        request_snapshot={"purpose": "generation"},
        response_snapshot={"content": MARKER, "tool_calls": []},
        input_tokens=100,
        output_tokens=10,
        accounted_total_tokens=110,
    )
    snapshot = RecoverySnapshot(run, (call,), (), ())
    compatibility = RecoveryCompatibility(RECOVERY_RUNTIME_VERSION, (), (), installation, None)
    result = resolve_recovery(snapshot, compatibility, datetime.now(UTC))
    assert result.action is RecoveryAction.STOP
    assert result.reason == "inconsistent_execution_records"
    assert snapshot.model_calls[0].accounted_total_tokens == 110
