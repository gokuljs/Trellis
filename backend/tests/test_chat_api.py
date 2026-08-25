import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.domain.models import Message, ProviderName
from app.infrastructure.providers import ProviderError
from app.main import create_app


class RecordingProvider:
    name: ProviderName = "openai"
    model = "recording-model"

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = replies or ["First reply", "Second reply"]
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        messages: Sequence[Message],
        api_key: str,
        user_id: str,
    ) -> str:
        self.calls.append(
            {
                "history": [(message.role, message.content) for message in messages],
                "api_key": api_key,
                "user_id": user_id,
            }
        )
        return self.replies[len(self.calls) - 1]


class FailingProvider(RecordingProvider):
    def __init__(self, error_code: str) -> None:
        super().__init__()
        self.error_code = error_code

    async def complete(
        self,
        messages: Sequence[Message],
        api_key: str,
        user_id: str,
    ) -> str:
        await super().complete(messages, api_key, user_id)
        raise ProviderError(self.error_code, "Sanitized provider failure")


class BlockingProvider(RecordingProvider):
    def __init__(self) -> None:
        super().__init__(["Released reply"])
        self.started = threading.Event()
        self.release = threading.Event()

    async def complete(
        self,
        messages: Sequence[Message],
        api_key: str,
        user_id: str,
    ) -> str:
        import asyncio

        self.started.set()
        await asyncio.to_thread(self.release.wait)
        return await super().complete(messages, api_key, user_id)


class FlakyProvider(RecordingProvider):
    async def complete(
        self,
        messages: Sequence[Message],
        api_key: str,
        user_id: str,
    ) -> str:
        if not self.calls:
            await super().complete(messages, api_key, user_id)
            raise ProviderError("provider_timeout", "Sanitized provider failure")
        return await super().complete(messages, api_key, user_id)


def configured_client(tmp_path: Path, provider: RecordingProvider) -> TestClient:
    app = create_app(
        Settings(environment="test", data_dir=tmp_path),
        provider_adapters={"openai": provider},
    )
    return TestClient(app)


def test_turns_persist_complete_ordered_history_and_restore_after_restart(tmp_path: Path) -> None:
    provider = RecordingProvider()
    first_turn_id = str(uuid4())
    second_turn_id = str(uuid4())

    with configured_client(tmp_path, provider) as client:
        client.put(
            "/api/settings/providers/openai/api-key",
            json={"api_key": "sk-local-test"},
        )
        profile_id = client.get("/api/profile").json()["id"]
        session = client.post("/api/sessions").json()
        first_turn = client.post(
            f"/api/sessions/{session['id']}/turns",
            json={"turn_id": first_turn_id, "content": "  Trace   this codebase  "},
        )
        second_turn = client.post(
            f"/api/sessions/{session['id']}/turns",
            json={"turn_id": second_turn_id, "content": "What should I inspect next?"},
        )

    with configured_client(tmp_path, RecordingProvider()) as restarted_client:
        restored = restarted_client.get(f"/api/sessions/{session['id']}")

    assert first_turn.status_code == 201
    assert first_turn.json()["user_message"]["content"] == "Trace   this codebase"
    assert first_turn.json()["assistant_message"]["content"] == "First reply"
    assert second_turn.status_code == 201
    assert provider.calls == [
        {
            "history": [("user", "Trace   this codebase")],
            "api_key": "sk-local-test",
            "user_id": profile_id,
        },
        {
            "history": [
                ("user", "Trace   this codebase"),
                ("assistant", "First reply"),
                ("user", "What should I inspect next?"),
            ],
            "api_key": "sk-local-test",
            "user_id": profile_id,
        },
    ]
    assert restored.status_code == 200
    assert restored.json()["session"]["title"] == "Trace this codebase"
    assert restored.json()["session"]["message_count"] == 4
    assert [message["content"] for message in restored.json()["messages"]] == [
        "Trace   this codebase",
        "First reply",
        "What should I inspect next?",
        "Second reply",
    ]


def test_duplicate_turn_id_returns_existing_messages_without_calling_provider_twice(
    tmp_path: Path,
) -> None:
    provider = RecordingProvider(["Only reply"])
    turn_id = str(uuid4())

    with configured_client(tmp_path, provider) as client:
        client.put(
            "/api/settings/providers/openai/api-key",
            json={"api_key": "sk-local-test"},
        )
        session_id = client.post("/api/sessions").json()["id"]
        first = client.post(
            f"/api/sessions/{session_id}/turns",
            json={"turn_id": turn_id, "content": "Persist this once"},
        )
        duplicate = client.post(
            f"/api/sessions/{session_id}/turns",
            json={"turn_id": turn_id, "content": "Persist this once"},
        )

    assert duplicate.status_code == 201
    assert duplicate.json() == first.json()
    assert len(provider.calls) == 1


def test_duplicate_turn_id_rejects_different_content(tmp_path: Path) -> None:
    provider = RecordingProvider(["Only reply"])
    turn_id = str(uuid4())

    with configured_client(tmp_path, provider) as client:
        client.put(
            "/api/settings/providers/openai/api-key",
            json={"api_key": "sk-local-test"},
        )
        session_id = client.post("/api/sessions").json()["id"]
        client.post(
            f"/api/sessions/{session_id}/turns",
            json={"turn_id": turn_id, "content": "Original"},
        )
        conflict = client.post(
            f"/api/sessions/{session_id}/turns",
            json={"turn_id": turn_id, "content": "Changed"},
        )

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "turn_conflict"


def test_failed_turn_resumes_without_duplicating_the_user_message(tmp_path: Path) -> None:
    provider = FlakyProvider(["unused", "Recovered reply"])
    turn_id = str(uuid4())

    with configured_client(tmp_path, provider) as client:
        client.put(
            "/api/settings/providers/openai/api-key",
            json={"api_key": "sk-local-test"},
        )
        session_id = client.post("/api/sessions").json()["id"]
        failed = client.post(
            f"/api/sessions/{session_id}/turns",
            json={"turn_id": turn_id, "content": "Resume me"},
        )
        resumed = client.post(
            f"/api/sessions/{session_id}/turns",
            json={"turn_id": turn_id, "content": "Resume me"},
        )
        detail = client.get(f"/api/sessions/{session_id}")

    assert failed.status_code == 504
    assert resumed.status_code == 201
    assert [message["content"] for message in detail.json()["messages"]] == [
        "Resume me",
        "Recovered reply",
    ]
    assert provider.calls[1]["history"] == [("user", "Resume me")]


def test_first_message_title_is_normalized_and_capped_at_eighty_characters(
    tmp_path: Path,
) -> None:
    provider = RecordingProvider(["Done"])
    content = "  " + "A   long title " * 10

    with configured_client(tmp_path, provider) as client:
        client.put(
            "/api/settings/providers/openai/api-key",
            json={"api_key": "sk-local-test"},
        )
        session_id = client.post("/api/sessions").json()["id"]
        response = client.post(
            f"/api/sessions/{session_id}/turns",
            json={"turn_id": str(uuid4()), "content": content},
        )

    assert response.status_code == 201
    assert response.json()["session"]["title"] == " ".join(content.split())[:80]
    assert len(response.json()["session"]["title"]) == 80


def test_missing_provider_key_does_not_persist_user_message(tmp_path: Path) -> None:
    provider = RecordingProvider()

    with configured_client(tmp_path, provider) as client:
        session_id = client.post("/api/sessions").json()["id"]
        response = client.post(
            f"/api/sessions/{session_id}/turns",
            json={"turn_id": str(uuid4()), "content": "Do not save this"},
        )
        detail = client.get(f"/api/sessions/{session_id}")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "provider_not_configured"
    assert detail.json()["messages"] == []
    assert provider.calls == []


def test_whitespace_only_message_is_rejected_without_persistence(tmp_path: Path) -> None:
    provider = RecordingProvider()

    with configured_client(tmp_path, provider) as client:
        client.put(
            "/api/settings/providers/openai/api-key",
            json={"api_key": "sk-local-test"},
        )
        session_id = client.post("/api/sessions").json()["id"]
        response = client.post(
            f"/api/sessions/{session_id}/turns",
            json={"turn_id": str(uuid4()), "content": "   \n\t "},
        )
        detail = client.get(f"/api/sessions/{session_id}")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "message_empty"
    assert detail.json()["messages"] == []
    assert provider.calls == []


@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [
        ("provider_auth_failed", 502),
        ("provider_rate_limited", 429),
        ("provider_timeout", 504),
        ("provider_upstream_failed", 502),
    ],
)
def test_provider_failures_preserve_only_the_user_message(
    tmp_path: Path,
    error_code: str,
    expected_status: int,
) -> None:
    provider = FailingProvider(error_code)

    with configured_client(tmp_path, provider) as client:
        client.put(
            "/api/settings/providers/openai/api-key",
            json={"api_key": "sk-local-test"},
        )
        session_id = client.post("/api/sessions").json()["id"]
        response = client.post(
            f"/api/sessions/{session_id}/turns",
            json={"turn_id": str(uuid4()), "content": "Keep this message"},
        )
        detail = client.get(f"/api/sessions/{session_id}")

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == error_code
    assert [message["content"] for message in detail.json()["messages"]] == ["Keep this message"]


def test_empty_provider_response_is_sanitized_and_preserves_user_message(tmp_path: Path) -> None:
    provider = RecordingProvider(["   "])

    with configured_client(tmp_path, provider) as client:
        client.put(
            "/api/settings/providers/openai/api-key",
            json={"api_key": "sk-local-test"},
        )
        session_id = client.post("/api/sessions").json()["id"]
        response = client.post(
            f"/api/sessions/{session_id}/turns",
            json={"turn_id": str(uuid4()), "content": "Keep this too"},
        )
        detail = client.get(f"/api/sessions/{session_id}")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_invalid_response"
    assert [message["content"] for message in detail.json()["messages"]] == ["Keep this too"]


def test_second_concurrent_turn_for_same_session_is_rejected(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    provider = BlockingProvider()
    with configured_client(tmp_path, provider) as client:
        client.put(
            "/api/settings/providers/openai/api-key",
            json={"api_key": "sk-local-test"},
        )
        session_id = client.post("/api/sessions").json()["id"]

        with ThreadPoolExecutor(max_workers=1) as executor:
            first_future = executor.submit(
                client.post,
                f"/api/sessions/{session_id}/turns",
                json={"turn_id": str(uuid4()), "content": "First"},
            )
            assert provider.started.wait(timeout=2)
            second = client.post(
                f"/api/sessions/{session_id}/turns",
                json={"turn_id": str(uuid4()), "content": "Second"},
            )
            provider.release.set()
            first = first_future.result(timeout=2)

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "turn_in_progress"


def test_turn_claim_is_shared_across_application_instances(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    provider = BlockingProvider()
    first_app = create_app(
        Settings(environment="test", data_dir=tmp_path),
        provider_adapters={"openai": provider},
    )
    second_app = create_app(
        Settings(environment="test", data_dir=tmp_path),
        provider_adapters={"openai": RecordingProvider()},
    )

    with TestClient(first_app) as first_client, TestClient(second_app) as second_client:
        first_client.put(
            "/api/settings/providers/openai/api-key",
            json={"api_key": "sk-local-test"},
        )
        session_id = first_client.post("/api/sessions").json()["id"]
        with ThreadPoolExecutor(max_workers=1) as executor:
            first_future = executor.submit(
                first_client.post,
                f"/api/sessions/{session_id}/turns",
                json={"turn_id": str(uuid4()), "content": "First process"},
            )
            assert provider.started.wait(timeout=2)
            second = second_client.post(
                f"/api/sessions/{session_id}/turns",
                json={"turn_id": str(uuid4()), "content": "Second process"},
            )
            provider.release.set()
            first = first_future.result(timeout=2)

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "turn_in_progress"
