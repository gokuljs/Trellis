"""The default server can handle chat without initializing local capabilities."""

from collections.abc import Sequence
from pathlib import Path
from typing import Never
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.domain.models import Message
from app.infrastructure import accounts
from app.infrastructure.command_tools import LocalCommandToolExecutor
from app.infrastructure.local_patch import LocalPatchToolExecutor
from app.infrastructure.local_tools import LocalReadToolExecutor
from app.infrastructure.workspace_bindings import LocalWorkspaceBindings
from app.infrastructure.workspace_picker import NativeFolderPicker
from tests.support import TestClient, create_app


class ChatProvider:
    name = "openai"
    model = "gpt-5.5"

    async def complete(self, messages: Sequence[Message], api_key: str, user_id: str) -> str:
        assert messages[-1].content == "Hello without a folder"
        assert api_key == "sk-chat-only-test"
        assert user_id
        return "Chat is available."


def test_default_chat_settings_and_sessions_do_not_initialize_local_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def local_access_forbidden(*_args: object, **_kwargs: object) -> Never:
        raise AssertionError("A disabled local capability was initialized or accessed")

    for local_type in (
        NativeFolderPicker,
        LocalReadToolExecutor,
        LocalPatchToolExecutor,
        LocalCommandToolExecutor,
        LocalWorkspaceBindings,
    ):
        monkeypatch.setattr(local_type, "__init__", local_access_forbidden)
    monkeypatch.setattr(accounts, "read_workspace_guidance", local_access_forbidden)

    app = create_app(
        Settings(environment="test", data_dir=tmp_path, openai_api_key="sk-chat-only-test"),
        provider_adapters={"openai": ChatProvider()},
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        settings = client.get("/api/settings")
        assert settings.status_code == 200
        assert settings.json()["capabilities"] == []

        created = client.post("/api/sessions")
        assert created.status_code == 201
        session = created.json()
        assert session["workspace_path"] is None
        assert session["workspace_ready"] is False

        completed = client.post(
            f"/api/sessions/{session['id']}/turns",
            json={"turn_id": str(uuid4()), "content": "Hello without a folder"},
        )
        assert completed.status_code == 201
        assert completed.json()["assistant_message"]["content"] == "Chat is available."
        detail = client.get(f"/api/sessions/{session['id']}")
        assert detail.status_code == 200
        assert [message["role"] for message in detail.json()["messages"]] == [
            "user",
            "assistant",
        ]
        assert detail.json()["session"]["workspace_path"] is None
        assert detail.json()["session"]["workspace_ready"] is False
