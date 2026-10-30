import asyncio
import copy
import json
from dataclasses import asdict
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from test_retrieval_worker import DEPLOYMENT, context, inspection

from app.domain.retrieval import MAX_EXTRACTED_RESULT_BYTES, ExtractedDocumentResult, RenderRequest
from app.domain.web import SourceStatus, WebUsage
from app.infrastructure import retrieval_worker as worker
from app.infrastructure.web_gateway import PublicGateway


def browser_inspection() -> dict:
    observed = inspection()
    observed["Config"]["Cmd"] = ["python", "-m", "app.infrastructure.web_renderer"]
    observed["HostConfig"].update(
        SecurityOpt=["no-new-privileges", "seccomp=" + worker._BROWSER_PROFILE.read_text()],
        Memory=2048 * 1024 * 1024,
        MemorySwap=2048 * 1024 * 1024,
        NanoCpus=2_000_000_000,
        PidsLimit=256,
        ShmSize=256 * 1024 * 1024,
        Tmpfs={"/tmp": "rw,noexec,nosuid,nodev,size=256m,mode=1777"},
    )
    return observed


def rendered_payload(request: RenderRequest, requests: int = 1) -> bytes:
    return json.dumps(
        asdict(
            ExtractedDocumentResult(
                SourceStatus.SUCCESS,
                request.url,
                request.url,
                "text/html",
                "Rendered evidence.",
                WebUsage(
                    page_attempts=1,
                    network_requests=requests,
                    response_bytes=100,
                    extracted_bytes=18,
                ),
                extractor_version="trellis-render-v1",
            )
        )
    ).encode()


def image_payload() -> bytes:
    return json.dumps(
        [
            {
                "Config": {
                    "Labels": {
                        "trellis.browser.protocol": "public-render-v1",
                        "trellis.browser.seccomp-sha256": worker._BROWSER_PROFILE_SHA256,
                    }
                }
            }
        ]
    ).encode()


@pytest.mark.parametrize(
    "key,value",
    [
        ("CapAdd", ["SYS_ADMIN"]),
        ("NetworkMode", "bridge"),
        ("IpcMode", "host"),
        ("Memory", 0),
        ("PidsLimit", 0),
        ("ShmSize", 0),
        ("SecurityOpt", ["no-new-privileges", 'seccomp={"defaultAction":"SCMP_ACT_ALLOW"}']),
        ("SecurityOpt", ["no-new-privileges", "seccomp=unconfined"]),
    ],
)
def test_browser_inspection_rejects_weakened_boundary(key, value):
    observed = browser_inspection()
    worker._inspect_worker(observed, DEPLOYMENT, render=True)
    observed["HostConfig"][key] = value
    with pytest.raises(worker.WorkerBoundaryError):
        worker._inspect_worker(observed, DEPLOYMENT, render=True)


def test_browser_inspection_rejects_additional_mount_and_raw_worker_command():
    observed = browser_inspection()
    observed["Mounts"].append({"Destination": "/var/run/docker.sock", "RW": False})
    with pytest.raises(worker.WorkerBoundaryError):
        worker._inspect_worker(observed, DEPLOYMENT, render=True)
    observed = browser_inspection()
    observed["Config"]["Cmd"] = inspection()["Config"]["Cmd"]
    with pytest.raises(worker.WorkerBoundaryError):
        worker._inspect_worker(observed, DEPLOYMENT, render=True)


@pytest.mark.parametrize(
    "probe",
    [b"{}", b'{"protocol":"public-render-v1","sandbox":false}', b"invalid", b"[]"],
)
def test_render_readiness_requires_actual_sandbox_probe(monkeypatch, probe):
    async def exercise():
        adapter = worker.DockerPublicRetriever(DEPLOYMENT, AsyncMock())
        adapter.is_ready = AsyncMock(return_value=True)
        cast(Any, adapter._control).is_render_ready = AsyncMock(return_value=True)
        adapter._inspect = AsyncMock(return_value=browser_inspection())
        calls = []

        async def docker(args, **kwargs):
            calls.append(args[0])
            if args[0] == "image":
                return 0, image_payload()
            if args[0] == "start":
                assert json.loads(kwargs["payload"]) == {"operation": "sandbox_probe"}
                return 0, probe
            return 0, b""

        monkeypatch.setattr(worker, "_run_docker", docker)
        assert await adapter.is_render_ready() is False
        assert calls == ["image", "create", "start", "rm"]
        assert not adapter._concurrency.proved_renderers

    asyncio.run(exercise())


def test_many_accounts_share_one_actual_probe_and_cache_only_after_cleanup(monkeypatch):
    async def exercise():
        concurrency = worker.RetrievalConcurrency()
        adapters = [
            worker.DockerPublicRetriever(DEPLOYMENT, AsyncMock(), concurrency=concurrency)
            for _ in range(16)
        ]
        for adapter in adapters:
            cast(Any, adapter._control).is_render_ready = AsyncMock(return_value=True)
            adapter.is_ready = AsyncMock(return_value=True)
            adapter._inspect = AsyncMock(return_value=browser_inspection())
        probing, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        starts = 0

        async def docker(args, **kwargs):
            nonlocal starts
            if args[0] == "image":
                return 0, image_payload()
            if args[0] == "start":
                starts += 1
                probing.set()
                return 0, b'{"protocol":"public-render-v1","sandbox":true}'
            if args[0] == "rm":
                cleaning.set()
                await release.wait()
            return 0, b""

        monkeypatch.setattr(worker, "_run_docker", docker)
        tasks = [asyncio.create_task(adapter.is_render_ready()) for adapter in adapters]
        async with asyncio.timeout(2):
            await probing.wait()
            await cleaning.wait()
            assert starts == 1
            assert not concurrency.proved_renderers
            release.set()
            assert await asyncio.gather(*tasks) == [True] * 16
        assert starts == 1
        assert len(concurrency.proved_renderers) == 1

    asyncio.run(exercise())


def test_gate_allows_two_raw_operations_and_one_render_without_leaking_cancelled_slots():
    async def exercise():
        gate = worker.RetrievalConcurrency()
        started, release = asyncio.Event(), asyncio.Event()
        active = peak = 0

        async def raw():
            nonlocal active, peak
            async with gate.operation(render=False):
                active += 1
                peak = max(peak, active)
                if active == 2:
                    started.set()
                await release.wait()
                active -= 1

        tasks = [asyncio.create_task(raw()) for _ in range(16)]
        async with asyncio.timeout(2):
            await started.wait()
            assert active == 2
            release.set()
            await asyncio.gather(*tasks)
        assert peak == 2
        async with gate.operation(render=False):
            blocked = asyncio.create_task(gate.operation(render=True).__aenter__())
            for _ in range(3):
                await asyncio.sleep(0)
            assert not blocked.done()
            blocked.cancel()
            with pytest.raises(asyncio.CancelledError):
                await blocked
            async with asyncio.timeout(1), gate.operation(render=False):
                pass
        async with asyncio.timeout(1), gate.operation(render=True):
            pass

    asyncio.run(exercise())


@pytest.mark.parametrize("cancel", [False, True])
def test_render_holds_shared_slots_until_cleanup_and_settles_authoritative_requests(
    monkeypatch, cancel
):
    async def exercise():
        request = RenderRequest("https://example.com")
        gate = worker.RetrievalConcurrency()
        control = AsyncMock()
        control.issue.return_value = "lease-token"
        cleaning, release, raw_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def revoke(token):
            cleaning.set()
            await release.wait()
            return worker.GatewayReceipt(2, 6000)

        control.revoke.side_effect = revoke
        adapter = worker.DockerPublicRetriever(DEPLOYMENT, control, concurrency=gate)
        adapter.is_render_ready = AsyncMock(return_value=True)
        adapter._inspect = AsyncMock(return_value=browser_inspection())

        async def docker(args, **kwargs):
            if args[0] == "start":
                assert json.loads(kwargs["payload"])["operation"] == "render"
                assert kwargs["limit"] == MAX_EXTRACTED_RESULT_BYTES
                return 0, rendered_payload(request, requests=75)
            return 0, b""

        async def raw():
            async with gate.operation(render=False):
                raw_started.set()

        monkeypatch.setattr(worker, "_run_docker", docker)
        render = asyncio.create_task(adapter.render_document(request, context()))
        async with asyncio.timeout(2):
            await cleaning.wait()
            other = asyncio.create_task(raw())
            await asyncio.sleep(0)
            assert not raw_started.is_set()
            if cancel:
                render.cancel()
                await asyncio.sleep(0)
                assert not render.done()
                assert not raw_started.is_set()
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await render
            else:
                result = await render
                assert result.status is SourceStatus.SUCCESS
                assert result.usage == WebUsage(
                    page_attempts=1, network_requests=75, response_bytes=6000, extracted_bytes=18
                )
            await other
        assert raw_started.is_set()

    asyncio.run(exercise())


def test_render_receipt_cannot_exceed_reserved_requests():
    request = RenderRequest("https://example.com")
    with pytest.raises(worker.WorkerBoundaryError):
        worker.DockerPublicRetriever._settled_usage(request, worker.GatewayReceipt(76, 1), True)


def test_render_document_output_keeps_identity_text_and_request_limits():
    request = RenderRequest("https://example.com")
    original = json.loads(rendered_payload(request))
    assert worker._decode_document(rendered_payload(request), request).text == "Rendered evidence."
    for change in (
        {"requested_url": "https://other.example"},
        {"text": "x" * (256 * 1024 + 1)},
        {"usage": asdict(WebUsage(page_attempts=1, network_requests=76, extracted_bytes=18))},
        {"body": "raw content"},
    ):
        value = copy.deepcopy(original)
        value.update(change)
        with pytest.raises(worker.WorkerBoundaryError):
            worker._decode_document(json.dumps(value).encode(), request)
    with pytest.raises(worker.WorkerBoundaryError):
        worker._decode_document(b" " * (MAX_EXTRACTED_RESULT_BYTES + 1), request)


def test_only_render_lease_accepts_sixty_seconds_with_seventy_five_requests():
    async def exercise():
        gateway = PublicGateway("s" * 48)
        try:
            with pytest.raises(ValueError):
                gateway.issue(60, 75, 1024)
            token = gateway.issue(60, 75, 1024, render=True)
            assert await gateway.revoke(token) == {"tunnels": 0, "transferred_bytes": 0}
            for duration, requests in ((60.01, 75), (60, 76)):
                with pytest.raises(ValueError):
                    gateway.issue(duration, requests, 1024, render=True)
            with pytest.raises(ValueError):
                gateway.issue(60, 75, 1024, render=cast(Any, 1))
        finally:
            await gateway.close()

    asyncio.run(exercise())


def test_old_gateway_cannot_advertise_renderer_even_with_cached_chromium_probe(monkeypatch):
    async def exercise():
        gate = worker.RetrievalConcurrency()
        gate.proved_renderers.add((DEPLOYMENT.worker_image, worker._BROWSER_PROFILE_SHA256))
        control = AsyncMock()
        control.is_render_ready.return_value = False
        adapter = worker.DockerPublicRetriever(DEPLOYMENT, control, concurrency=gate)
        adapter.is_ready = AsyncMock(return_value=True)
        docker = AsyncMock()
        monkeypatch.setattr(worker, "_run_docker", docker)
        assert await adapter.is_render_ready() is False
        docker.assert_not_awaited()
        control.is_render_ready.assert_awaited_once()

    asyncio.run(exercise())


def test_render_readiness_control_handshake_is_explicit_and_keeps_raw_protocol():
    async def exercise():
        secret = "s" * 48
        gateway = PublicGateway(secret)
        server = await asyncio.start_server(gateway.handle_admin, "127.0.0.1", 0)
        control = worker.GatewayControlClient(
            f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", secret
        )
        async with server:
            assert await control.is_ready() is True
            assert await control.is_render_ready() is True
        await gateway.close()

    asyncio.run(exercise())
