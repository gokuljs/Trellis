import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fastapi import WebSocket, WebSocketDisconnect

from app.api.routes import runtime as runtime_routes
from app.application.runs import RunService
from app.domain.runtime import RunEvent, RunEventType, RunSnapshot, RunStatus
from app.infrastructure.secrets import SecretStore
from tests.memory_repository import MemoryRepository
from tests.test_runtime_cleanup import PeerSocket, RecordingHub, assert_runtime_children_gone


async def _append_deltas(repository: MemoryRepository, run_id: str, count: int) -> None:
    for _ in range(count):
        await repository.append_run_event(run_id, RunEventType.ASSISTANT_DELTA, {"text": "x"})


async def _finish(repository: MemoryRepository, run_id: str) -> None:
    await repository.transition_run_record(
        run_id,
        RunStatus.FAILED,
        RunEventType.FAILED,
        {"code": "provider_upstream_failed", "message": "The provider stopped."},
    )


@pytest.mark.parametrize("mode", ["initial", "poll", "duplicate_live", "terminal_during_poll"])
def test_replay_drains_all_durable_pages_before_terminal_close(tmp_path: Path, mode: str) -> None:
    class RacingRepository(MemoryRepository):
        race_armed = False
        last_cursor = 0
        page_lengths: list[int]

        async def list_run_events(
            self, run_id: str, after_sequence: int = 0, *, limit: int = 500
        ) -> list[RunEvent]:
            self.last_cursor = after_sequence
            page = await super().list_run_events(run_id, after_sequence, limit=limit)
            self.page_lengths.append(len(page))
            return page

        async def get_run(self, run_id: str) -> RunSnapshot | None:
            if self.race_armed and self.last_cursor == 2:
                self.race_armed = False
                await _append_deltas(self, run_id, 702)
                await _finish(self, run_id)
            return await super().get_run(run_id)

    async def exercise() -> None:
        owner_id = UUID("00000000-0000-0000-0000-000000000001")
        repository = RacingRepository(tmp_path / "memory", owner_id=owner_id)
        repository.page_lengths = []
        session = await repository.create_session()
        run = await repository.create_run(
            session.id,
            "replay-turn",
            "replay-request",
            "Answer",
            (await repository.list_models())[0],
        )
        await repository.transition_run_record(
            run.id, RunStatus.RUNNING, RunEventType.STARTED, {"status": "running"}
        )
        if mode == "initial":
            await _append_deltas(repository, run.id, 1201)
            await _finish(repository, run.id)

        initial_sent = asyncio.Event()
        terminal_sent = asyncio.Event()
        notifications: list[dict[str, object]] = []
        sent_resume = False
        populated = mode == "initial"
        hub = RecordingHub()
        service = RunService(
            repository,
            repository,
            repository,
            repository,
            SecretStore(tmp_path / ".env"),
            {},
            hub,
        )

        async def receive_more() -> str:
            nonlocal sent_resume, populated
            if not sent_resume:
                sent_resume = True
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "resume-pages",
                        "method": "run.resume",
                        "params": {"runId": run.id, "afterSequence": 0},
                    }
                )
            await asyncio.wait_for(initial_sent.wait(), timeout=1)
            if not populated:
                populated = True
                if mode == "terminal_during_poll":
                    await _append_deltas(repository, run.id, 499)
                    repository.race_armed = True
                else:
                    await _append_deltas(repository, run.id, 1201)
                    await _finish(repository, run.id)
                if mode == "duplicate_live":
                    for event in repository.state.events[run.id][:2]:
                        await hub.publish(event)
            with suppress(TimeoutError):
                await asyncio.wait_for(terminal_sent.wait(), timeout=2)
            raise WebSocketDisconnect(code=1000)

        async def send_payload(payload: object) -> None:
            if isinstance(payload, dict) and payload.get("method") == "run.event":
                params = cast(dict[str, object], payload["params"])
                notifications.append(params)
                if params["sequence"] == 2:
                    initial_sent.set()
                if params["eventType"] == "run.failed":
                    terminal_sent.set()

        socket = PeerSocket(hub, receive_more, send_payload=send_payload, resume_run=False)
        account = await socket.app.state.account_registry.get(owner_id)
        account.run_service = service
        await runtime_routes.runtime_websocket(cast(WebSocket, socket))
        await service.close()
        assert [item["sequence"] for item in notifications] == list(range(1, 1205))
        assert notifications[-1]["eventType"] == "run.failed"
        assert max(repository.page_lengths) <= 500
        assert_runtime_children_gone(hub)

    asyncio.run(exercise())
