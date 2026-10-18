import asyncio
import json

import httpx

from app.domain.runtime import RunStatus
from app.infrastructure.supabase_repository import SupabaseRepository
from tests.support import verified_token_source
from tests.test_supabase_repository import ALICE, SETTINGS, _run_row


def test_durable_owner_can_cancel_without_an_execution_capability() -> None:
    requests = []
    row = {**_run_row(), "status": "cancelling"}

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"run": row, "event": None})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            repository = SupabaseRepository(
                SETTINGS.model_copy(update={"durable_execution_enabled": True}),
                ALICE,
                verified_token_source(ALICE),
                client,
            )
            run, event = await repository.request_run_cancellation(str(row["id"]))
            assert run.status is RunStatus.CANCELLING and event is None
        assert requests == [
            {
                "p_action": "request_run_cancellation",
                "p_payload": {"run_id": row["id"]},
                "p_capability": None,
            }
        ]

    asyncio.run(scenario())
