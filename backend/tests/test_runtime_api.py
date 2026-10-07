import asyncio
import sqlite3
import threading
from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.application.tools import ToolRegistry
from app.core.config import Settings
from app.domain.models import ProviderName
from app.domain.runtime import ModelRequest, ModelStreamEvent, ModelToolCall, RunEvent, RunEventType
from app.infrastructure.local_tools import LocalReadToolExecutor
from app.infrastructure.runtime_events import RuntimeEventHub
from app.main import create_app


class StreamingProvider:
    name: ProviderName = "openai"
    model = "custom-model-alias"

    def __init__(self) -> None:
        self.request: ModelRequest | None = None

    async def complete(self, *_args: object, **_kwargs: object) -> str:
        raise NotImplementedError

    def stream(
        self,
        request: ModelRequest,
        api_key: str,
        user_id: str,
    ) -> AsyncGenerator[ModelStreamEvent]:
        self.request = request
        del api_key, user_id

        async def generate() -> AsyncGenerator[ModelStreamEvent]:
            yield ModelStreamEvent(kind="text_delta", text="Streaming")
            yield ModelStreamEvent(kind="text_delta", text=" works")
            yield ModelStreamEvent(
                kind="usage",
                input_tokens=4,
                output_tokens=2,
                provider_response_id="resp-jsonrpc",
            )
            yield ModelStreamEvent(kind="completed", finish_reason="stop")

        return generate()


class BlockingStreamingProvider(StreamingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()

    def stream(
        self,
        request: ModelRequest,
        api_key: str,
        user_id: str,
    ) -> AsyncGenerator[ModelStreamEvent]:
        self.request = request

        async def generate() -> AsyncGenerator[ModelStreamEvent]:
            yield ModelStreamEvent(kind="text_delta", text="Working")
            self.started.set()
            await asyncio.Event().wait()

        return generate()


class GappedStreamingProvider(StreamingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.continue_stream = threading.Event()

    def stream(
        self,
        request: ModelRequest,
        api_key: str,
        user_id: str,
    ) -> AsyncGenerator[ModelStreamEvent]:
        self.request = request
        del api_key, user_id

        async def generate() -> AsyncGenerator[ModelStreamEvent]:
            yield ModelStreamEvent(kind="text_delta", text="dropped delta")
            yield ModelStreamEvent(kind="usage", input_tokens=1, output_tokens=1)
            await asyncio.to_thread(self.continue_stream.wait)
            yield ModelStreamEvent(kind="text_delta", text="replayed delta")
            yield ModelStreamEvent(kind="completed", finish_reason="stop")

        return generate()


class ApprovalStreamingProvider(StreamingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def stream(
        self, request: ModelRequest, api_key: str, user_id: str
    ) -> AsyncGenerator[ModelStreamEvent]:
        self.request = request
        self.calls += 1
        call_number = self.calls
        del api_key, user_id

        async def generate() -> AsyncGenerator[ModelStreamEvent]:
            if call_number == 1:
                yield ModelStreamEvent(
                    kind="tool_call",
                    tool_call=ModelToolCall("provider-read", "read_file", {"path": "note.txt"}),
                )
                yield ModelStreamEvent(kind="usage", input_tokens=1, output_tokens=1)
                yield ModelStreamEvent(kind="completed", finish_reason="tool_use")
            else:
                yield ModelStreamEvent(kind="text_delta", text="The note was read.")
                yield ModelStreamEvent(kind="usage", input_tokens=1, output_tokens=1)
                yield ModelStreamEvent(kind="completed", finish_reason="stop")

        return generate()


def configured_client(tmp_path: Path, provider: StreamingProvider) -> TestClient:
    app = create_app(
        Settings(environment="test", data_dir=tmp_path),
        provider_adapters={"openai": provider},
    )
    return TestClient(app)


def receive_until(websocket, predicate):
    received = []
    while True:
        message = websocket.receive_json()
        received.append(message)
        if predicate(message):
            return received


def test_jsonrpc_run_respond_approves_exact_waiting_tool_and_resumes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "note.txt").write_text("safe contents\n", encoding="utf-8")
    provider = ApprovalStreamingProvider()
    app = create_app(
        Settings(environment="test", data_dir=tmp_path / "data"),
        provider_adapters={"openai": provider},
        tool_registry=ToolRegistry(LocalReadToolExecutor(), approval_required_names={"read_file"}),
    )
    with TestClient(app) as client:
        client.put("/api/settings/providers/openai/api-key", json={"api_key": "sk-approval-test"})
        session_id = client.post("/api/sessions", json={"workspace_path": str(project)}).json()[
            "id"
        ]
        with client.websocket_connect("/api/runtime") as websocket:
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "start",
                    "method": "run.start",
                    "params": {
                        "sessionId": session_id,
                        "clientRequestId": "approval-request",
                        "content": "Read the note",
                    },
                }
            )
            waiting_events = receive_until(
                websocket,
                lambda item: (
                    item.get("method") == "run.event"
                    and item["params"]["eventType"] == RunEventType.TOOL_APPROVAL_REQUESTED.value
                ),
            )
            run_id = next(
                item["result"]["runId"] for item in waiting_events if item.get("id") == "start"
            )
            request = waiting_events[-1]["params"]
            call_id = request["data"]["tool_call_id"]
            assert (
                client.put(
                    f"/api/sessions/{session_id}/workspace", json={"workspace_path": None}
                ).status_code
                == 409
            )
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "wrong",
                    "method": "run.respond",
                    "params": {"runId": run_id, "toolCallId": "wrong-tool", "decision": "approved"},
                }
            )
            wrong = websocket.receive_json()
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "invalid",
                    "method": "run.respond",
                    "params": {"runId": run_id, "toolCallId": call_id, "decision": "maybe"},
                }
            )
            invalid = websocket.receive_json()
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "approve",
                    "method": "run.respond",
                    "params": {"runId": run_id, "toolCallId": call_id, "decision": "approved"},
                }
            )
            resumed = receive_until(
                websocket,
                lambda item: (
                    item.get("method") == "run.event"
                    and item["params"]["eventType"] == RunEventType.COMPLETED.value
                ),
            )
            if not any(item.get("id") == "approve" for item in resumed):
                resumed.append(websocket.receive_json())
        detail = client.get(f"/api/sessions/{session_id}").json()

    assert wrong["error"]["data"]["code"] == "tool_call_not_found"
    assert invalid["error"]["code"] == -32602
    assert (
        next(item["result"]["toolCallId"] for item in resumed if item.get("id") == "approve")
        == call_id
    )
    assert [message["content"] for message in detail["messages"]] == [
        "Read the note",
        "The note was read.",
    ]
    assert provider.calls == 2


def test_jsonrpc_websocket_streams_a_durable_run(tmp_path: Path) -> None:
    provider = StreamingProvider()
    with configured_client(tmp_path, provider) as client:
        client.put(
            "/api/settings/providers/openai/api-key",
            json={"api_key": "sk-jsonrpc-test"},
        )
        session_id = client.post("/api/sessions").json()["id"]
        with client.websocket_connect("/api/runtime") as websocket:
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "start-1",
                    "method": "run.start",
                    "params": {
                        "sessionId": session_id,
                        "clientRequestId": "request-jsonrpc-1",
                        "content": "Stream this",
                    },
                }
            )
            received = receive_until(
                websocket,
                lambda item: (
                    item.get("method") == "run.event"
                    and item["params"]["eventType"] == RunEventType.COMPLETED.value
                ),
            )

        result = next(item["result"] for item in received if item.get("id") == "start-1")
        notifications = [item["params"] for item in received if item.get("method") == "run.event"]
        detail = client.get(f"/api/sessions/{session_id}").json()

    assert result["status"] == "queued"
    assert provider.request is not None
    assert provider.request.upstream_model_id == "gpt-5.5"
    assert [item["eventType"] for item in notifications] == [
        RunEventType.QUEUED.value,
        RunEventType.STARTED.value,
        RunEventType.MODEL_USAGE.value,
        RunEventType.MODEL_COMPLETED.value,
        RunEventType.ASSISTANT_DELTA.value,
        RunEventType.ASSISTANT_COMPLETED.value,
        RunEventType.COMPLETED.value,
    ]
    assert [item["sequence"] for item in notifications] == list(range(1, 8))
    assert [message["content"] for message in detail["messages"]] == [
        "Stream this",
        "Streaming works",
    ]


def test_jsonrpc_start_selects_a_budget_preset_and_rejects_unknown_names(tmp_path: Path) -> None:
    provider = StreamingProvider()
    with configured_client(tmp_path, provider) as client:
        client.put("/api/settings/providers/openai/api-key", json={"api_key": "sk-budget-test"})
        session_id = client.post("/api/sessions").json()["id"]
        with client.websocket_connect("/api/runtime") as websocket:
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "budget-start",
                    "method": "run.start",
                    "params": {
                        "sessionId": session_id,
                        "clientRequestId": "budget-request",
                        "content": "Answer",
                        "budgetPreset": "longer",
                    },
                }
            )
            received = receive_until(websocket, lambda item: item.get("id") == "budget-start")
            response = next(item for item in received if item.get("id") == "budget-start")
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "bad-budget",
                    "method": "run.start",
                    "params": {
                        "sessionId": session_id,
                        "clientRequestId": "another-request",
                        "content": "Answer",
                        "budgetPreset": "unlimited",
                    },
                }
            )
            rejected = receive_until(websocket, lambda item: item.get("id") == "bad-budget")
            error = next(item for item in rejected if item.get("id") == "bad-budget")
        persisted = asyncio.run(client.app.state.database.get_run(response["result"]["runId"]))

    assert response["result"]["budgetPreset"] == "longer"
    assert response["result"]["limits"]["maxTotalTokens"] == 200_000
    assert persisted is not None and persisted.budget_preset == "longer"
    assert error["error"] == {"code": -32602, "message": "Invalid params: budgetPreset"}


def test_jsonrpc_resume_replays_persisted_events_after_disconnect(tmp_path: Path) -> None:
    provider = StreamingProvider()
    with configured_client(tmp_path, provider) as client:
        client.put(
            "/api/settings/providers/openai/api-key",
            json={"api_key": "sk-jsonrpc-test"},
        )
        session_id = client.post("/api/sessions").json()["id"]
        with client.websocket_connect("/api/runtime") as websocket:
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "run.start",
                    "params": {
                        "sessionId": session_id,
                        "clientRequestId": "request-jsonrpc-resume",
                        "content": "Replay this",
                    },
                }
            )
            first_messages = receive_until(
                websocket,
                lambda item: item.get("method") == "run.event" and item["params"]["sequence"] >= 4,
            )
            if not any(item.get("id") == 1 for item in first_messages):
                first_messages.extend(receive_until(websocket, lambda item: item.get("id") == 1))
            run_id = next(item["result"]["runId"] for item in first_messages if item.get("id") == 1)

        with client.websocket_connect("/api/runtime") as websocket:
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "run.resume",
                    "params": {"runId": run_id, "afterSequence": 4},
                }
            )
            resumed = receive_until(
                websocket,
                lambda item: (
                    item.get("method") == "run.event"
                    and item["params"]["eventType"] == RunEventType.COMPLETED.value
                ),
            )

    acknowledgement = next(item for item in resumed if item.get("id") == 2)
    events = [item["params"] for item in resumed if item.get("method") == "run.event"]
    assert acknowledgement["result"]["runId"] == run_id
    assert [event["sequence"] for event in events] == list(range(5, 8))


def test_jsonrpc_replays_when_a_live_event_sequence_has_a_gap(tmp_path: Path) -> None:
    provider = GappedStreamingProvider()
    with configured_client(tmp_path, provider) as client:
        client.put(
            "/api/settings/providers/openai/api-key",
            json={"api_key": "sk-jsonrpc-gap"},
        )
        event_hub: RuntimeEventHub = client.app.state.runtime_event_hub
        dropped = threading.Event()

        class DropFirstUsage:
            def __init__(self) -> None:
                self.has_dropped = False

            async def publish(self, event: RunEvent) -> None:
                if event.event_type is RunEventType.MODEL_USAGE and not self.has_dropped:
                    self.has_dropped = True
                    dropped.set()
                    return
                await event_hub.publish(event)

        client.app.state.run_service._event_publisher = DropFirstUsage()
        session_id = client.post("/api/sessions").json()["id"]
        with client.websocket_connect("/api/runtime") as websocket:
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "gap-start",
                    "method": "run.start",
                    "params": {
                        "sessionId": session_id,
                        "clientRequestId": "request-gap-replay",
                        "content": "Replay the missing delta",
                    },
                }
            )
            initial = receive_until(
                websocket,
                lambda item: (
                    item.get("method") == "run.event"
                    and item["params"]["eventType"] == RunEventType.STARTED.value
                ),
            )
            result = next(item["result"] for item in initial if item.get("id") == "gap-start")
            assert dropped.wait(timeout=5)
            provider.continue_stream.set()
            replay_request = receive_until(
                websocket,
                lambda item: item.get("method") == "run.replay_required",
            )[-1]
            cursor = replay_request["params"]["afterSequence"]
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "gap-resume",
                    "method": "run.resume",
                    "params": {"runId": result["runId"], "afterSequence": cursor},
                }
            )
            replayed = receive_until(
                websocket,
                lambda item: (
                    item.get("method") == "run.event"
                    and item["params"]["eventType"] == RunEventType.COMPLETED.value
                ),
            )

    events = [item["params"] for item in replayed if item.get("method") == "run.event"]
    assert cursor == 2
    assert [item["sequence"] for item in events] == list(range(3, 8))


def test_jsonrpc_reports_parse_empty_batch_and_valid_batch_responses(tmp_path: Path) -> None:
    with (
        configured_client(tmp_path, StreamingProvider()) as client,
        client.websocket_connect("/api/runtime") as websocket,
    ):
        websocket.send_text("not-json")
        parse_error = websocket.receive_json()
        websocket.send_json([])
        empty_batch_error = websocket.receive_json()
        websocket.send_json(
            [
                {"jsonrpc": "2.0", "id": 7, "method": "unsupported"},
                {"jsonrpc": "2.0", "method": "notification"},
                {"jsonrpc": "2.0", "id": None, "method": "also-unsupported"},
            ]
        )
        batch_responses = websocket.receive_json()

    assert parse_error["error"]["code"] == -32700
    assert empty_batch_error["error"]["code"] == -32600
    assert [response["id"] for response in batch_responses] == [7, None]
    assert [response["error"]["code"] for response in batch_responses] == [-32601, -32601]


def test_custom_model_uses_registered_adapter_without_provider_specific_runtime_code(
    tmp_path: Path,
) -> None:
    settings = Settings(environment="test", data_dir=tmp_path)
    provider = StreamingProvider()
    app = create_app(
        settings,
        streaming_provider_adapters={"openai-compatible": provider},
    )
    with TestClient(app) as client:
        client.get("/api/profile")
        with sqlite3.connect(settings.database_path) as connection:
            connection.execute(
                """INSERT INTO models(
                       id, provider_id, provider_name, adapter_kind, upstream_model_id,
                       name, requires_api_key, supports_streaming, supports_tools, enabled,
                       created_at, updated_at
                   ) VALUES (
                       'weights:v4', 'self-hosted', 'Self hosted', 'openai-compatible',
                       'weights/model-v4', 'Model v4', 0, 1, 0, 1,
                       '2026-01-01', '2026-01-01'
                   )"""
            )
            connection.execute(
                "UPDATE app_settings SET selected_provider = ?, selected_model_id = ? WHERE id = 1",
                ("self-hosted", "weights:v4"),
            )
        session_id = client.post("/api/sessions").json()["id"]
        with client.websocket_connect("/api/runtime") as websocket:
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "custom-model-run",
                    "method": "run.start",
                    "params": {
                        "sessionId": session_id,
                        "clientRequestId": "request-custom-model",
                        "content": "Use my local model",
                    },
                }
            )
            receive_until(
                websocket,
                lambda item: (
                    item.get("method") == "run.event"
                    and item["params"]["eventType"] == RunEventType.COMPLETED.value
                ),
            )
        messages = client.get(f"/api/sessions/{session_id}").json()["messages"]

    assert provider.request is not None
    assert provider.request.provider_id == "self-hosted"
    assert provider.request.model_id == "weights:v4"
    assert provider.request.adapter_kind == "openai-compatible"
    assert provider.request.upstream_model_id == "weights/model-v4"
    assert messages[-1]["provider"] == "self-hosted"
    assert messages[-1]["model"] == "weights:v4"


def test_unpriced_custom_model_stops_before_a_tool_continuation(tmp_path: Path) -> None:
    class ToolingProvider(StreamingProvider):
        def stream(
            self, request: ModelRequest, api_key: str, user_id: str
        ) -> AsyncGenerator[ModelStreamEvent]:
            self.request = request

            async def generate() -> AsyncGenerator[ModelStreamEvent]:
                yield ModelStreamEvent(
                    kind="tool_call", tool_call=ModelToolCall("custom-call", "list_files", {})
                )
                yield ModelStreamEvent(kind="usage", input_tokens=5, output_tokens=2)
                yield ModelStreamEvent(kind="completed", finish_reason="tool_use")

            return generate()

    settings = Settings(environment="test", data_dir=tmp_path / "data")
    provider = ToolingProvider()
    app = create_app(settings, streaming_provider_adapters={"openai-compatible": provider})
    with TestClient(app) as client:
        with sqlite3.connect(settings.database_path) as connection:
            connection.execute(
                """INSERT INTO models(
                       id, provider_id, provider_name, adapter_kind, upstream_model_id,
                       name, requires_api_key, supports_streaming, supports_tools, enabled,
                       created_at, updated_at
                   ) VALUES (
                       'weights:tooling', 'self-hosted', 'Self hosted', 'openai-compatible',
                       'weights/tooling', 'Tooling', 0, 1, 1, 1,
                       '2026-01-01', '2026-01-01'
                   )"""
            )
            connection.execute(
                "UPDATE app_settings SET selected_provider = ?, selected_model_id = ? WHERE id = 1",
                ("self-hosted", "weights:tooling"),
            )
        session_id = client.post("/api/sessions", json={"workspace_path": str(tmp_path)}).json()[
            "id"
        ]
        with client.websocket_connect("/api/runtime") as websocket:
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "custom-tool-run",
                    "method": "run.start",
                    "params": {
                        "sessionId": session_id,
                        "clientRequestId": "custom-tool-request",
                        "content": "Inspect files",
                    },
                }
            )
            events = receive_until(
                websocket,
                lambda item: (
                    item.get("method") == "run.event"
                    and item["params"]["eventType"] == RunEventType.FAILED.value
                ),
            )
        run_id = next(item["result"]["runId"] for item in events if item.get("id"))
        run = asyncio.run(client.app.state.database.get_run(run_id))
        calls = asyncio.run(client.app.state.database.list_model_calls(run_id))
        tools = asyncio.run(client.app.state.database.list_tool_calls(run_id))

    assert run is not None and run.error_code == "pricing_unavailable"
    assert len(calls) == 1 and calls[0].estimated_cost is None
    assert tools == []
    assert (
        next(
            item["params"]["data"]["estimated_cost_usd"]
            for item in events
            if item.get("method") == "run.event"
            and item["params"]["eventType"] == RunEventType.MODEL_USAGE.value
        )
        is None
    )


def test_jsonrpc_run_start_rejects_invalid_session_with_protocol_error(tmp_path: Path) -> None:
    with (
        configured_client(tmp_path, StreamingProvider()) as client,
        client.websocket_connect("/api/runtime") as websocket,
    ):
        websocket.send_json(
            {
                "jsonrpc": "2.0",
                "id": str(uuid4()),
                "method": "run.start",
                "params": {
                    "sessionId": "missing-session",
                    "clientRequestId": "request-invalid-session",
                    "content": "Hello",
                },
            }
        )
        response = websocket.receive_json()

    assert response["error"]["code"] == -32000
    assert response["error"]["data"]["code"] == "session_not_found"


def test_event_hub_closes_a_slow_subscriber_for_persisted_replay() -> None:
    hub = RuntimeEventHub(max_pending_events=1)
    subscription = hub.subscribe("run-1")
    first = RunEvent("run-1", 1, RunEventType.STARTED, 1, {}, "created-1")
    second = RunEvent("run-1", 2, RunEventType.ASSISTANT_DELTA, 1, {}, "created-2")

    async def publish() -> RunEvent | None:
        await hub.publish(first)
        await hub.publish(second)
        return await subscription.receive()

    assert asyncio.run(publish()) is None
    hub.unsubscribe(subscription)
    assert hub._subscribers == {}


def test_jsonrpc_can_cancel_a_streaming_run(tmp_path: Path) -> None:
    provider = BlockingStreamingProvider()
    with configured_client(tmp_path, provider) as client:
        client.put(
            "/api/settings/providers/openai/api-key",
            json={"api_key": "sk-jsonrpc-test"},
        )
        session_id = client.post("/api/sessions").json()["id"]
        with client.websocket_connect("/api/runtime") as websocket:
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "start-cancel",
                    "method": "run.start",
                    "params": {
                        "sessionId": session_id,
                        "clientRequestId": "request-cancel-api",
                        "content": "Stop this run",
                    },
                }
            )
            started = receive_until(
                websocket,
                lambda item: (
                    item.get("method") == "run.event"
                    and item["params"]["eventType"] == RunEventType.STARTED.value
                ),
            )
            assert provider.started.wait(timeout=5)
            run_id = next(
                item["result"]["runId"] for item in started if item.get("id") == "start-cancel"
            )
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "cancel-1",
                    "method": "run.cancel",
                    "params": {"runId": run_id},
                }
            )
            cancelled = receive_until(
                websocket,
                lambda item: (
                    item.get("method") == "run.event"
                    and item["params"]["eventType"] == RunEventType.CANCELLED.value
                ),
            )

    assert any(
        item.get("id") == "cancel-1" and item["result"]["status"] == "cancelling"
        for item in cancelled
    )
    assert any(
        item.get("method") == "run.event"
        and item["params"]["eventType"] == RunEventType.CANCELLATION_REQUESTED.value
        for item in cancelled
    )


def test_jsonrpc_rejects_resume_cursor_ahead_of_current_run(tmp_path: Path) -> None:
    with configured_client(tmp_path, StreamingProvider()) as client:
        client.put(
            "/api/settings/providers/openai/api-key",
            json={"api_key": "sk-jsonrpc-test"},
        )
        session_id = client.post("/api/sessions").json()["id"]
        with client.websocket_connect("/api/runtime") as websocket:
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "start-for-cursor",
                    "method": "run.start",
                    "params": {
                        "sessionId": session_id,
                        "clientRequestId": "request-cursor-validation",
                        "content": "Create a run",
                    },
                }
            )
            started = receive_until(websocket, lambda item: item.get("id") == "start-for-cursor")
            run_id = next(item["result"]["runId"] for item in started if item.get("id"))
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "invalid-cursor",
                    "method": "run.resume",
                    "params": {"runId": run_id, "afterSequence": 999},
                }
            )
            response = receive_until(
                websocket,
                lambda item: item.get("id") == "invalid-cursor",
            )[-1]

    assert response["id"] == "invalid-cursor"
    assert response["error"]["code"] == -32602
