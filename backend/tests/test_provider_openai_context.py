import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from app.application.context_budget import input_fingerprint, preflight_context
from app.application.errors import ApplicationError, ProviderError
from app.domain.context import InputTokenCount
from app.infrastructure.providers import OpenAIProvider
from tests.test_context_budget import item, profile
from tests.test_context_budget import request as context_request
from tests.test_provider_counting import request


def test_openai_native_compaction_keeps_every_returned_item_and_reported_usage() -> None:
    window = [
        {"role": "user", "content": "retained user text"},
        {"type": "compaction", "id": "cmp1", "encrypted_content": "opaque", "future": "unchanged"},
    ]
    captured: list[httpx.Request] = []

    async def exercise() -> None:

        def respond(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(
                200,
                json={
                    "id": "compact1",
                    "output": window,
                    "service_tier": "priority",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 25,
                        "input_tokens_details": {"cached_tokens": 40},
                        "output_tokens_details": {"reasoning_tokens": 5},
                    },
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await OpenAIProvider(client).compact(request("openai"), "key", "user")
        assert result.payload_json is not None and json.loads(result.payload_json) == window
        assert result.native_kind == "openai_window" and result.failure_code is None
        assert (
            result.provider_response_id == "compact1" and result.usage_events[0].input_tokens == 100
        )
        assert result.usage_events[0].service_tier == "priority"
        assert (
            result.usage_events[0].cached_tokens == 40
            and result.usage_events[0].reasoning_tokens == 5
        )

    asyncio.run(exercise())
    assert captured[0].url.path == "/v1/responses/compact"
    body = json.loads(captured[0].content)
    assert "tools" not in body
    assert "stream" not in body and "store" not in body and ("max_output_tokens" not in body)


def test_native_payload_over_limit_is_discarded_whole_but_usage_is_preserved() -> None:

    async def exercise() -> None:
        payload = {
            "output": [{"type": "compaction", "encrypted_content": "x" * 262145}],
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=payload))
        ) as client:
            adapter = OpenAIProvider(client)
            result = await adapter.compact(request("openai"), "key", "user")
        assert result.payload_json is None and result.failure_code == "native_context_too_large"
        assert result.usage_events[0].input_tokens == 100

    asyncio.run(exercise())


def test_native_context_is_in_fingerprint_and_cannot_use_byte_estimate() -> None:
    items = (item("m1", "now", "user", "Continue", required=True),)
    original = context_request(items)
    native = replace(
        original, native_context_json='[{"type":"compaction","encrypted_content":"opaque"}]'
    )
    assert input_fingerprint(native) != input_fingerprint(original)
    with pytest.raises(ApplicationError, match="provider token count"):
        preflight_context(native, items=items, profile=profile(), remaining_run_tokens=100000)
    plan = preflight_context(
        native,
        items=items,
        profile=profile(),
        remaining_run_tokens=100000,
        counted_input=InputTokenCount(100, "provider", input_fingerprint(native)),
    )
    assert plan.input_tokens == 100


def test_native_context_is_replayed_first_on_count_requests() -> None:
    captured: list[httpx.Request] = []
    native = [
        {"role": "user", "content": "retained"},
        {"type": "compaction", "encrypted_content": "opaque"},
    ]

    async def exercise() -> None:

        def respond(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json={"input_tokens": 90})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            adapter = OpenAIProvider(client)
            await adapter.count_input(
                replace(request("openai"), native_context_json=json.dumps(native)), "key", "user"
            )

    asyncio.run(exercise())
    body = json.loads(captured[0].content)
    assert body["input"][:2] == native and body["input"][2]["content"] == "Continue"


def test_native_signed_state_reaches_generation_without_modification() -> None:
    captured: list[httpx.Request] = []
    native = [
        {"role": "user", "content": "retained"},
        {"type": "compaction", "encrypted_content": "opaque"},
    ]
    events = [
        {"type": "response.output_text.delta", "delta": "Next"},
        {
            "type": "response.completed",
            "response": {"id": "res", "usage": {"input_tokens": 3, "output_tokens": 1}},
        },
    ]

    async def exercise() -> None:

        def respond(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(
                200, content="".join(f"data: {json.dumps(event)}\n\n" for event in events)
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            adapter = OpenAIProvider(client)
            result = [
                event
                async for event in adapter.stream(
                    replace(request("openai"), native_context_json=json.dumps(native)),
                    "key",
                    "user",
                )
            ]
        assert result[-1].finish_kind == "final"

    asyncio.run(exercise())
    body = json.loads(captured[0].content)
    assert body["input"][:2] == native


@pytest.mark.parametrize(
    "payload",
    [
        "[",
        '[{"type":"compaction","encrypted_content":NaN}]',
        '[{"type":"compaction","encrypted_content":"' + "x" * 262145 + '"}]',
        '{"type":"compaction","content":"wrong adapter","signature":"signed"}',
    ],
    ids=["json", "nan", "oversized", "adapter"],
)
def test_malformed_native_input_fails_before_sending_provider_request(payload: str) -> None:
    sent = []

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: sent.append(req) or httpx.Response(200, json={"input_tokens": 1})
            )
        ) as client:
            with pytest.raises(ProviderError):
                await OpenAIProvider(client).count_input(
                    replace(request("openai"), native_context_json=payload), "key", "user"
                )

    asyncio.run(exercise())
    assert sent == []


def test_cancellation_during_native_response_closes_the_http_stream() -> None:

    class SlowBody(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.closed = False

        async def __aiter__(self):
            self.started.set()
            yield b'{"output":'
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            self.closed = True

    async def exercise() -> None:
        body = SlowBody()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, stream=body))
        ) as client:
            task = asyncio.create_task(
                OpenAIProvider(client).compact(request("openai"), "key", "user")
            )
            await body.started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert body.closed

    asyncio.run(exercise())


def test_portable_summary_request_does_not_inherit_opaque_native_state() -> None:
    from app.application.compaction import build_summary_request

    source = item("old", "old-turn", "user", "Historical text")
    original = replace(
        request("openai"),
        native_context_json='[{"type":"compaction","encrypted_content":"opaque"}]',
    )
    summary = build_summary_request(original, (source,))
    assert summary.native_context_json is None
    assert "opaque" not in summary.messages[0].content


@pytest.mark.parametrize(
    "payload",
    [
        {"output": [{"type": "compaction", "encrypted_content": "opaque"}], "usage": {}},
        {"output": [], "usage": {"input_tokens": 100, "output_tokens": 10}},
        {
            "status": "incomplete",
            "output": [{"type": "compaction", "encrypted_content": "opaque"}],
            "usage": {"input_tokens": 100, "output_tokens": 10},
        },
    ],
)
def test_unusable_native_result_never_discards_its_reported_usage(
    payload: dict[str, object],
) -> None:

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
        ) as client:
            result = await OpenAIProvider(client).compact(request("openai"), "key", "user")
        assert result.payload_json is None and result.failure_code is not None
        if payload["usage"]:
            assert result.usage_events[0].input_tokens == 100

    asyncio.run(exercise())


def test_native_usage_keeps_counts_but_replaces_malformed_service_tier() -> None:
    async def exercise() -> None:
        payload = {
            "output": [{"type": "compaction", "encrypted_content": "opaque"}],
            "service_tier": "untrusted provider metadata with spaces",
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
        ) as client:
            result = await OpenAIProvider(client).compact(request("openai"), "key", "user")
        assert result.usage_events[0].input_tokens == 100
        assert result.usage_events[0].service_tier == "unknown"

    asyncio.run(exercise())
