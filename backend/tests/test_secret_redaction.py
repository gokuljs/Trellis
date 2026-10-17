import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from app.application.chat import ChatService
from app.application.errors import ApplicationError, ProviderError
from app.application.runs import RunService
from app.application.secret_redaction import SecretRedactor
from app.application.tools import ToolRegistration, ToolRegistry
from app.domain.capabilities import RuntimeCapabilities
from app.domain.runtime import (
    ModelContinuationItem,
    ModelStreamEvent,
    ModelToolCall,
    RunEventType,
    RunStatus,
    ToolCallStatus,
)
from app.infrastructure.local_tools import LocalReadToolExecutor
from app.infrastructure.secrets import SecretStore
from app.infrastructure.workspace_bindings import LocalWorkspaceBindings
from tests.memory_repository import MemoryRepository
from tests.test_chat_api import RecordingProvider
from tests.test_runtime_service import SequencedProvider

CONFIGURED_SECRET = "operator-credential-without-recognizable-prefix"
SECOND_SECRET = "second-operator-private-value"


def test_workspace_guidance_redacts_configured_literal_before_model_context(tmp_path: Path) -> None:
    database = MemoryRepository(tmp_path / "database")
    secrets = SecretStore(tmp_path / ".env")
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(kind="text_delta", text="Done"),
                ModelStreamEvent(kind="completed", finish_reason="stop"),
            ),
        )
    )

    async def guidance(workspace_root: Path) -> str:
        return f"Workspace note: {CONFIGURED_SECRET}"

    async def exercise() -> None:
        session = await database.create_session(str(tmp_path))
        bindings = LocalWorkspaceBindings()
        bindings.bind(session.id, tmp_path)
        await secrets.set("openai", CONFIGURED_SECRET)
        service = RunService(
            database,
            database,
            database,
            database,
            secrets,
            {"openai": provider},
            capabilities=RuntimeCapabilities(frozenset({"local_read"})),
            workspace_bindings=bindings,
            workspace_guidance_reader=guidance,
            redactor=SecretRedactor(CONFIGURED_SECRET),
        )
        try:
            run = await service.create_run(session.id, "guidance-secret", "Read workspace note")
            await service.wait_for_run(run.id)
            assert CONFIGURED_SECRET not in provider.requests[0].system_instructions
            assert "Workspace note: [REDACTED]" in provider.requests[0].system_instructions
            assert CONFIGURED_SECRET not in repr(database.state)
        finally:
            await service.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("secret_source", ["arguments", "result", "preview"])
def test_runtime_redacts_configured_literals_across_injected_tool_exchange(
    tmp_path: Path, secret_source: str
) -> None:
    class LookupArguments(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        query: str

    class LookupExecutor:
        async def execute(
            self, name: str, arguments: dict[str, object], workspace_root: Path | None
        ) -> tuple[str, bool]:
            return f"Tool result: {CONFIGURED_SECRET}", False

    class PreviewRegistry(ToolRegistry):
        async def approval_preview(self, call, workspace_root, *, capabilities=None):
            return {"preview": CONFIGURED_SECRET}

    database = MemoryRepository(tmp_path / "database")
    secrets = SecretStore(tmp_path / ".env")
    registry_type = PreviewRegistry if secret_source == "preview" else ToolRegistry
    registry = registry_type(
        additional_tools=(
            ToolRegistration("lookup", "Look up a record", LookupArguments, LookupExecutor()),
        ),
        approval_required_names={"lookup"} if secret_source == "preview" else frozenset(),
    )
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(
                    kind="tool_call",
                    tool_call=ModelToolCall(
                        "lookup-call",
                        "lookup",
                        {"query": CONFIGURED_SECRET if secret_source == "arguments" else "safe"},
                    ),
                ),
                ModelStreamEvent(kind="completed", finish_reason="tool_use"),
            ),
            (
                ModelStreamEvent(kind="text_delta", text="Done"),
                ModelStreamEvent(kind="completed", finish_reason="stop"),
            ),
        )
    )

    async def exercise() -> None:
        session = await database.create_session()
        await secrets.set("openai", CONFIGURED_SECRET)
        service = RunService(
            database,
            database,
            database,
            database,
            secrets,
            {"openai": provider},
            redactor=SecretRedactor(CONFIGURED_SECRET),
            tool_registry=registry,
        )
        try:
            run = await service.create_run(session.id, "tool-secret", "Look up record")
            await service.wait_for_run(run.id)
            finished = await database.get_run(run.id)
            assert finished is not None and finished.status is RunStatus.COMPLETED
            assert CONFIGURED_SECRET not in repr(database.state)
            assert CONFIGURED_SECRET not in provider.requests[1].messages[-1].content
            calls = await database.list_tool_calls(run.id)
            events = await database.list_run_events(run.id)
            assert RunEventType.TOOL_APPROVAL_REQUESTED not in [
                event.event_type for event in events
            ]
            if secret_source == "arguments":
                assert calls[0].status is ToolCallStatus.FAILED
                assert "sensitive_tool_arguments" in provider.requests[1].messages[-1].content
            elif secret_source == "preview":
                assert calls[0].status is ToolCallStatus.FAILED
                assert "sensitive_approval_preview" in provider.requests[1].messages[-1].content
            else:
                assert calls[0].status is ToolCallStatus.COMPLETED
                assert "[REDACTED]" in provider.requests[1].messages[-1].content
        finally:
            await service.close()

    asyncio.run(exercise())


def test_event_publisher_exception_does_not_log_configured_literal(tmp_path: Path, caplog) -> None:
    database = MemoryRepository(tmp_path / "database")
    secrets = SecretStore(tmp_path / ".env")
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(kind="text_delta", text="Done"),
                ModelStreamEvent(kind="completed", finish_reason="stop"),
            ),
        )
    )

    class BrokenPublisher:
        async def publish(self, event):
            raise RuntimeError(f"Publisher detail {CONFIGURED_SECRET}")

    async def exercise() -> None:
        session = await database.create_session()
        await secrets.set("openai", CONFIGURED_SECRET)
        service = RunService(
            database,
            database,
            database,
            database,
            secrets,
            {"openai": provider},
            BrokenPublisher(),
            redactor=SecretRedactor(CONFIGURED_SECRET),
        )
        try:
            run = await service.create_run(session.id, "publisher-secret", "Hello")
            await service.wait_for_run(run.id)
            finished = await database.get_run(run.id)
            assert finished is not None and finished.status is RunStatus.COMPLETED
            assert CONFIGURED_SECRET not in caplog.text
        finally:
            await service.close()

    asyncio.run(exercise())


def test_tool_registry_redacts_configured_literal_in_result(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text(f"Private: {CONFIGURED_SECRET}", encoding="utf-8")
    registry = ToolRegistry(
        LocalReadToolExecutor(),
        capabilities=RuntimeCapabilities(frozenset({"local_read"})),
        redactor=SecretRedactor(CONFIGURED_SECRET),
    )
    result = asyncio.run(
        registry.execute(ModelToolCall("read", "read_file", {"path": "note.txt"}), tmp_path)
    )
    assert not result.is_error
    assert CONFIGURED_SECRET not in result.content
    assert "[REDACTED]" in result.content


@pytest.mark.parametrize("runtime", [False, True])
def test_chat_paths_redact_both_configured_literals(tmp_path: Path, runtime: bool) -> None:
    database = MemoryRepository(tmp_path / "database")
    secrets = SecretStore(tmp_path / ".env")
    redactor = SecretRedactor(CONFIGURED_SECRET, SECOND_SECRET)
    reply = f"Provider returned {SECOND_SECRET}"

    async def exercise() -> None:
        session = await database.create_session()
        await secrets.set("openai", CONFIGURED_SECRET)
        if runtime:
            provider = SequencedProvider(
                (
                    (
                        ModelStreamEvent(kind="text_delta", text=reply),
                        ModelStreamEvent(
                            kind="usage",
                            input_tokens=1,
                            output_tokens=1,
                            provider_response_id=SECOND_SECRET,
                        ),
                        ModelStreamEvent(kind="completed", finish_reason=SECOND_SECRET),
                    ),
                )
            )
            service = RunService(
                database,
                database,
                database,
                database,
                secrets,
                {"openai": provider},
                redactor=redactor,
            )
            try:
                run = await service.create_run(
                    session.id, "literal-request", f"Inspect {CONFIGURED_SECRET}"
                )
                await service.wait_for_run(run.id)
                finished = await database.get_run(run.id)
                assert finished is not None and finished.status is RunStatus.COMPLETED
            finally:
                await service.close()
        else:
            provider = RecordingProvider([reply])
            service = ChatService(database, secrets, {"openai": provider}, redactor=redactor)
            result = await service.complete_turn(
                session.id, "literal-turn", f"Inspect {CONFIGURED_SECRET}"
            )
            assert SECOND_SECRET not in repr(result)
        assert CONFIGURED_SECRET not in repr(database.state)
        assert SECOND_SECRET not in repr(database.state)

    asyncio.run(exercise())


@pytest.mark.parametrize("runtime", [False, True])
def test_known_provider_errors_do_not_persist_or_expose_configured_literals(
    tmp_path: Path, runtime: bool, caplog
) -> None:
    database = MemoryRepository(tmp_path / "database")
    secrets = SecretStore(tmp_path / ".env")
    redactor = SecretRedactor(CONFIGURED_SECRET)

    class FailureProvider(RecordingProvider):
        async def complete(self, messages, api_key, user_id):
            raise ProviderError(
                "provider_upstream_failed", f"Failure containing {CONFIGURED_SECRET}"
            )

        def stream(self, request, api_key, user_id):
            async def generate():
                raise ProviderError(
                    "provider_upstream_failed", f"Failure containing {CONFIGURED_SECRET}"
                )
                yield ModelStreamEvent(kind="completed")

            return generate()

    async def exercise() -> None:
        session = await database.create_session()
        await secrets.set("openai", CONFIGURED_SECRET)
        provider = FailureProvider()
        if runtime:
            service = RunService(
                database,
                database,
                database,
                database,
                secrets,
                {"openai": provider},
                redactor=redactor,
            )
            try:
                run = await service.create_run(session.id, "failure-request", "Hello")
                await service.wait_for_run(run.id)
                finished = await database.get_run(run.id)
                assert finished is not None and finished.error_code == "provider_upstream_failed"
                assert CONFIGURED_SECRET not in (finished.error_message or "")
            finally:
                await service.close()
        else:
            service = ChatService(database, secrets, {"openai": provider}, redactor=redactor)
            with pytest.raises(ApplicationError) as error:
                await service.complete_turn(session.id, "failure-turn", "Hello")
            assert CONFIGURED_SECRET not in error.value.message
        assert CONFIGURED_SECRET not in repr(database.state)
        assert CONFIGURED_SECRET not in caplog.text

    asyncio.run(exercise())


def test_runtime_rejects_configured_literals_in_opaque_continuation(tmp_path: Path) -> None:
    database = MemoryRepository(tmp_path / "database")
    secrets = SecretStore(tmp_path / ".env")
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(
                    kind="continuation_item",
                    continuation_item=ModelContinuationItem(
                        "openai", "openai:gpt-5.5", '{"opaque":"' + CONFIGURED_SECRET + '"}'
                    ),
                ),
                ModelStreamEvent(kind="text_delta", text="Done"),
                ModelStreamEvent(kind="completed", finish_reason="stop"),
            ),
        )
    )

    async def exercise() -> None:
        session = await database.create_session()
        await secrets.set("openai", CONFIGURED_SECRET)
        service = RunService(
            database,
            database,
            database,
            database,
            secrets,
            {"openai": provider},
            redactor=SecretRedactor(CONFIGURED_SECRET),
        )
        try:
            run = await service.create_run(session.id, "continuation-secret", "Hello")
            await service.wait_for_run(run.id)
            finished = await database.get_run(run.id)
            assert finished is not None and finished.status is RunStatus.FAILED
            assert finished.error_code == "provider_invalid_response"
            assert CONFIGURED_SECRET not in repr(database.state)
        finally:
            await service.close()

    asyncio.run(exercise())


def test_classic_chat_redacts_submitted_credentials_before_persistence(tmp_path: Path) -> None:
    database = MemoryRepository(tmp_path / "database")
    secrets = SecretStore(tmp_path / ".env")
    provider = RecordingProvider(["Your submitted API_KEY=user-plain-secret is protected."])

    async def exercise() -> None:
        session = await database.create_session()
        await secrets.set("openai", "sk-classic-provider-key")
        service = ChatService(database, secrets, {"openai": provider})
        result = await service.complete_turn(
            session.id, "turn-1", "Please use API_KEY=user-plain-secret"
        )
        assert "user-plain-secret" not in repr(result)
        assert "user-plain-secret" not in repr(database.state)
        assert result.user_message.content == "Please use API_KEY=[REDACTED]"
        assert result.assistant_message.content == "Your submitted API_KEY=[REDACTED] is protected."

    asyncio.run(exercise())


def test_runtime_redacts_submitted_credentials_in_messages_snapshots_and_events(
    tmp_path: Path,
) -> None:
    database = MemoryRepository(tmp_path / "database")
    secrets = SecretStore(tmp_path / ".env")
    provider = SequencedProvider(
        (
            (
                ModelStreamEvent(kind="text_delta", text="Echo API_KEY=user-plain-secret"),
                ModelStreamEvent(kind="completed", finish_reason="stop"),
            ),
        )
    )

    async def exercise() -> None:
        session = await database.create_session()
        await secrets.set("openai", "sk-runtime-provider-key")
        service = RunService(database, database, database, database, secrets, {"openai": provider})
        try:
            run = await service.create_run(
                session.id, "request-1", "Inspect API_KEY=user-plain-secret"
            )
            await service.wait_for_run(run.id)
            finished = await database.get_run(run.id)
            assert finished is not None and finished.status is RunStatus.COMPLETED
            assert "user-plain-secret" not in repr(database.state)
        finally:
            await service.close()

    asyncio.run(exercise())


def test_classic_chat_masks_unexpected_provider_exceptions(tmp_path: Path, caplog) -> None:
    database = MemoryRepository(tmp_path / "database")
    secrets = SecretStore(tmp_path / ".env")

    class BrokenProvider(RecordingProvider):
        async def complete(self, messages, api_key, user_id):
            raise RuntimeError("Private provider detail API_KEY=exception-secret")

    async def exercise() -> None:
        session = await database.create_session()
        await secrets.set("openai", "sk-classic-provider-key")
        service = ChatService(database, secrets, {"openai": BrokenProvider()})
        with pytest.raises(ApplicationError) as error:
            await service.complete_turn(session.id, "turn-1", "Hello")
        assert error.value.code == "provider_upstream_failed"
        assert "exception-secret" not in str(error.value)
        assert "exception-secret" not in caplog.text
        assert "exception-secret" not in repr(database.state)

    asyncio.run(exercise())
