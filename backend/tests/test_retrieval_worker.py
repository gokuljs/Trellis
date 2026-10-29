import asyncio
import base64
import copy
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.application.budgets import RunLimits
from app.application.tool_context import ToolExecutionContext
from app.domain.retrieval import RetrievalRequest, RetrievalStatus
from app.domain.web import WebUsage
from app.infrastructure import retrieval_worker as worker

DEPLOYMENT = worker.WorkerDeployment(
    "sha256:" + "a" * 64, "sha256:" + "b" * 64, "gateway", "egress-ipc", Path("/fixture/admin")
)


def context() -> ToolExecutionContext:
    return ToolExecutionContext(
        "owner",
        "session",
        "run",
        "tool",
        "dispatch",
        1,
        1,
        datetime.now(UTC) + timedelta(seconds=30),
        RunLimits(8, 16, 100000, 600, 2.0),
        None,
        AsyncMock(),
    )


def inspection() -> dict:
    return {
        "Image": DEPLOYMENT.worker_image,
        "Config": {
            "User": "10001:10001",
            "Cmd": ["python", "-m", "app.infrastructure.web_reader"],
            "Entrypoint": None,
        },
        "HostConfig": {
            "NetworkMode": "none",
            "IpcMode": "private",
            "ReadonlyRootfs": True,
            "Privileged": False,
            "CapDrop": ["ALL"],
            "CapAdd": None,
            "SecurityOpt": ["no-new-privileges"],
            "Init": True,
            "Memory": 512 * 1024 * 1024,
            "MemorySwap": 512 * 1024 * 1024,
            "NanoCpus": 1000000000,
            "PidsLimit": 64,
            "ShmSize": 32 * 1024 * 1024,
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=64m,mode=1777"},
        },
        "Mounts": [
            {
                "Type": "volume",
                "Name": "egress-ipc",
                "Destination": "/run/trellis-egress",
                "RW": False,
            }
        ],
    }


def result_payload(request: RetrievalRequest, **changes) -> bytes:
    result = {
        "status": "success",
        "requested_url": request.url,
        "final_url": request.url,
        "media_type": "text/html",
        "body": base64.b64encode(b"hello").decode(),
        "usage": asdict(WebUsage(page_attempts=1, network_requests=1, response_bytes=5)),
        "error_code": None,
    }
    result.update(changes)
    return json.dumps(result).encode()


@pytest.mark.parametrize("image", ["python:latest", "sha256:a", "x; touch /tmp/pwned"])
def test_images_must_be_pinned(image):
    with pytest.raises(ValueError, match="pinned"):
        worker.WorkerDeployment(
            image, DEPLOYMENT.gateway_image, "gateway", "ipc", Path("/fixture/admin")
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8081",
        "http://127.0.0.1.evil:8081",
        "https://127.0.0.1:8081",
        "http://user@127.0.0.1:8081",
        "http://127.0.0.1:8081/a",
    ],
)
def test_control_is_literal_loopback_only(url):
    with pytest.raises(ValueError, match="loopback"):
        worker.GatewayControlClient(url, "a" * 48)


@pytest.mark.parametrize(
    "key,value",
    [
        ("NetworkMode", "host"),
        ("ReadonlyRootfs", False),
        ("CapAdd", ["SYS_ADMIN"]),
        ("Privileged", True),
        ("Init", False),
        ("PidsLimit", 0),
        ("SecurityOpt", []),
        ("PortBindings", {"80/tcp": []}),
    ],
)
def test_actual_worker_inspection_rejects_weakened_boundary(key, value):
    observed = inspection()
    observed["HostConfig"][key] = value
    with pytest.raises(worker.WorkerBoundaryError):
        worker._inspect_worker(observed, DEPLOYMENT)


def test_actual_worker_rejects_secret_or_socket_mount():
    observed = inspection()
    worker._inspect_worker(observed, DEPLOYMENT)
    observed["Mounts"].append({"Destination": "/var/run/docker.sock", "RW": False})
    with pytest.raises(worker.WorkerBoundaryError):
        worker._inspect_worker(observed, DEPLOYMENT)


def test_worker_result_has_measured_allowance_and_identity():
    request = RetrievalRequest("https://example.com")
    assert worker._decode_result(result_payload(request), request).body == b"hello"
    for change in (
        {"requested_url": "https://other.test"},
        {"body": "%%%"},
        {"usage": asdict(WebUsage(page_attempts=1, network_requests=7))},
        {"status": "blocked", "body": "aGVsbG8="},
    ):
        with pytest.raises(worker.WorkerBoundaryError):
            worker._decode_result(result_payload(request, **change), request)


def test_cancellation_revokes_lease_and_removes_worker(monkeypatch):
    async def exercise():
        control = AsyncMock()
        control.issue.return_value = "lease-token"
        control.revoke.return_value = None
        adapter = worker.DockerPublicRetriever(DEPLOYMENT, control)
        adapter.is_ready = AsyncMock(return_value=True)
        adapter._inspect = AsyncMock(return_value=inspection())
        started = asyncio.Event()
        commands = []

        async def docker(args, **kwargs):
            commands.append(args)
            if args[0] == "start":
                assert json.loads(kwargs["payload"])["lease_token"] == "lease-token"
                started.set()
                await asyncio.Event().wait()
            return 0, b""

        monkeypatch.setattr(worker, "_run_docker", docker)
        task = asyncio.create_task(
            adapter.retrieve(RetrievalRequest("https://example.com"), context())
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        control.revoke.assert_awaited_once_with("lease-token")
        assert commands[-1][:2] == ("rm", "--force")

    asyncio.run(exercise())


def test_bad_inspection_never_starts_worker(monkeypatch):
    async def exercise():
        control = AsyncMock()
        control.issue.return_value = "lease-token"
        control.revoke.return_value = None
        adapter = worker.DockerPublicRetriever(DEPLOYMENT, control)
        adapter.is_ready = AsyncMock(return_value=True)
        unsafe = copy.deepcopy(inspection())
        unsafe["HostConfig"]["NetworkMode"] = "bridge"
        adapter._inspect = AsyncMock(return_value=unsafe)
        docker = AsyncMock(return_value=(0, b""))
        monkeypatch.setattr(worker, "_run_docker", docker)
        result = await adapter.retrieve(RetrievalRequest("https://example.com"), context())
        assert result.status is RetrievalStatus.UNAVAILABLE
        assert all(call.args[0][0] != "start" for call in docker.await_args_list)
        control.revoke.assert_awaited_once()

    asyncio.run(exercise())


def test_killed_worker_consumes_reserved_allowance(monkeypatch):
    async def exercise():
        control = AsyncMock()
        control.issue.return_value = "lease-token"
        control.revoke.return_value = None
        adapter = worker.DockerPublicRetriever(DEPLOYMENT, control)
        adapter.is_ready = AsyncMock(return_value=True)
        adapter._inspect = AsyncMock(return_value=inspection())

        async def docker(args, **_kwargs):
            if args[0] == "start":
                raise TimeoutError
            return 0, b""

        monkeypatch.setattr(worker, "_run_docker", docker)
        request = RetrievalRequest("https://example.com", max_requests=2, max_response_bytes=100)
        result = await adapter.retrieve(request, context())
        assert result.status is RetrievalStatus.TIMEOUT
        assert result.usage == WebUsage(page_attempts=1, network_requests=2, response_bytes=100)
        control.revoke.assert_awaited_once()

    asyncio.run(exercise())


def test_authoritative_gateway_receipt_replaces_worker_payload_usage(monkeypatch):
    async def exercise():
        request = RetrievalRequest("https://example.com")
        control = AsyncMock()
        control.issue.return_value = "lease-token"
        control.revoke.return_value = worker.GatewayReceipt(2, 6000)
        adapter = worker.DockerPublicRetriever(DEPLOYMENT, control)
        adapter.is_ready = AsyncMock(return_value=True)
        adapter._inspect = AsyncMock(return_value=inspection())

        async def docker(args, **_kwargs):
            return (0, result_payload(request)) if args[0] == "start" else (0, b"")

        monkeypatch.setattr(worker, "_run_docker", docker)
        result = await adapter.retrieve(request, context())
        assert result.status is RetrievalStatus.SUCCESS
        assert result.usage == WebUsage(page_attempts=1, network_requests=2, response_bytes=6000)

    asyncio.run(exercise())


def test_receipts_cannot_authorize_more_than_reserved():
    request = RetrievalRequest("https://example.com", max_requests=1, max_response_bytes=100)
    for receipt in (worker.GatewayReceipt(2, 100), worker.GatewayReceipt(1, 101)):
        with pytest.raises(worker.WorkerBoundaryError):
            worker.DockerPublicRetriever._settled_usage(request, receipt, True)
    assert worker.DockerPublicRetriever._settled_usage(request, None, True) == WebUsage(
        page_attempts=1, network_requests=1, response_bytes=100
    )


def test_control_issues_exact_reserved_byte_allowance():
    async def exercise():
        control = worker.GatewayControlClient("http://127.0.0.1:8081", "s" * 48)
        control._request = AsyncMock(return_value={"token": "t" * 43})
        request = RetrievalRequest("https://example.com", max_requests=2, max_response_bytes=100)
        assert await control.issue(request, 10) == "t" * 43
        control._request.assert_awaited_once_with(
            "POST", "/leases", {"ttl_seconds": 10, "max_tunnels": 2, "max_bytes": 100}
        )
        control._request.return_value = {
            "revoked": True,
            "receipt": {"tunnels": 1, "transferred_bytes": 90},
        }
        assert await control.revoke("t" * 43) == worker.GatewayReceipt(1, 90)

    asyncio.run(exercise())


def test_control_dribbling_response_has_total_deadline(monkeypatch):
    async def exercise():
        began = asyncio.Event()

        async def handle(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 200\r\n\r\n")
                await writer.drain()
                began.set()
                for _ in range(200):
                    writer.write(b" ")
                    await writer.drain()
                    await asyncio.sleep(0.01)
            except OSError:
                pass
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(worker, "_CONTROL_DEADLINE_SECONDS", 0.1)
        control = worker.GatewayControlClient(f"http://127.0.0.1:{port}", "s" * 48)
        async with server:
            with pytest.raises(TimeoutError):
                await control._request("GET", "/ready")
            assert began.is_set()

    asyncio.run(exercise())


def test_control_rejects_encoded_response_without_decoding():
    async def exercise():
        async def handle(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nContent-Encoding: gzip\r\n\r\nnope"
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        control = worker.GatewayControlClient(f"http://127.0.0.1:{port}", "s" * 48)
        async with server:
            with pytest.raises(worker.WorkerBoundaryError, match="encoded"):
                await control._request("GET", "/ready")

    asyncio.run(exercise())


@pytest.mark.parametrize("binding", [[], None, {}, [{"HostIp": "0.0.0.0", "HostPort": "1234"}]])
def test_gateway_readiness_fails_closed_on_invalid_port_bindings(binding):
    async def exercise():
        adapter = worker.DockerPublicRetriever(DEPLOYMENT, AsyncMock())
        adapter._inspect = AsyncMock(
            return_value={
                "HostConfig": {},
                "Mounts": [],
                "NetworkSettings": {"Ports": {"8081/tcp": binding}},
            }
        )
        assert await adapter.is_ready() is False

    asyncio.run(exercise())


def test_readiness_time_does_not_renew_the_gateway_lease(monkeypatch):
    async def exercise():
        request = RetrievalRequest("https://example.com", timeout_seconds=1)
        control = AsyncMock()
        control.issue.return_value = "lease-token"
        control.revoke.return_value = None
        adapter = worker.DockerPublicRetriever(DEPLOYMENT, control)

        async def ready():
            await asyncio.sleep(0.02)
            return True

        adapter.is_ready = AsyncMock(side_effect=ready)
        adapter._inspect = AsyncMock(return_value=inspection())

        async def docker(args, **_kwargs):
            return (0, result_payload(request)) if args[0] == "start" else (0, b"")

        monkeypatch.setattr(worker, "_run_docker", docker)
        assert (await adapter.retrieve(request, context())).status is RetrievalStatus.SUCCESS
        assert 0 < control.issue.await_args.args[1] < 0.99

    asyncio.run(exercise())
