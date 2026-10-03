import asyncio
import sqlite3
from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.domain.models import ProviderName
from app.domain.runtime import ModelRequest, ModelStreamEvent, RunEvent, RunEventType
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
        RunEventType.ASSISTANT_DELTA.value,
        RunEventType.ASSISTANT_DELTA.value,
        RunEventType.MODEL_USAGE.value,
        RunEventType.MODEL_COMPLETED.value,
        RunEventType.ASSISTANT_COMPLETED.value,
        RunEventType.COMPLETED.value,
    ]
    assert [item["sequence"] for item in notifications] == list(range(1, 9))
    assert [message["content"] for message in detail["messages"]] == [
        "Stream this",
        "Streaming works",
    ]


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
            first_messages = receive_until(websocket, lambda item: item.get("id") == 1)
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
    assert [event["sequence"] for event in events] == list(range(5, 9))


def test_jsonrpc_rejects_invalid_request_and_unsupported_batch(tmp_path: Path) -> None:
    with (
        configured_client(tmp_path, StreamingProvider()) as client,
        client.websocket_connect("/api/runtime") as websocket,
    ):
        websocket.send_text("not-json")
        parse_error = websocket.receive_json()
        websocket.send_json([])
        batch_error = websocket.receive_json()

    assert parse_error["error"]["code"] == -32700
    assert batch_error["error"]["code"] == -32600


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
