import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app.application.runs import RunService
from app.domain.runtime import RunStatus
from app.infrastructure.providers import OpenAIProvider
from app.infrastructure.secrets import SecretStore
from tests.memory_repository import MODELS, MemoryRepository


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"])
def test_verified_openai_models_complete_with_cache_write_usage(tmp_path: Path, model: str) -> None:
    async def exercise():
        def respond(_request):
            events = [
                {"type": "response.output_text.delta", "delta": "Answer"},
                {
                    "type": "response.completed",
                    "response": {
                        "id": "response-cache",
                        "usage": {
                            "input_tokens": 1000,
                            "output_tokens": 100,
                            "input_tokens_details": {
                                "cached_tokens": 200,
                                "cache_write_tokens": 300,
                            },
                        },
                    },
                },
            ]
            return httpx.Response(
                200, content="".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()
            )

        database = MemoryRepository(tmp_path / "memory")
        selected = replace(MODELS[0], id=f"openai:{model}", upstream_model_id=model)
        database.state.models.append(selected)
        secrets = SecretStore(tmp_path / ".env")
        await secrets.set("openai", "test-key")
        session = await database.create_session()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            service = RunService(
                database, database, database, database, secrets, {"openai": OpenAIProvider(client)}
            )
            try:
                run = await service.create_run(session.id, model, "Hi", model_id=selected.id)
                await service.wait_for_run(run.id)
                saved = await database.get_run(run.id)
                assert saved is not None and saved.status is RunStatus.COMPLETED
                call = (await database.list_model_calls(run.id))[0]
                assert call.cache_creation_tokens == 300
                assert call.estimated_cost is not None and call.estimated_cost > 0
                assert (await database.list_messages(session.id))[-1].content == "Answer"
            finally:
                await service.close()

    asyncio.run(exercise())
