"""Fixed, inspected Docker operations for untrusted public document processing."""

import asyncio
import base64
import binascii
import json
import re
import sys
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from app.application.tool_context import ToolExecutionContext
from app.domain.retrieval import (
    HTML_DECODED_BYTES,
    MAX_WORKER_REQUEST_BYTES,
    PDF_RAW_BYTES,
    RetrievalRequest,
    RetrievalResult,
    RetrievalStatus,
)
from app.domain.web import WebUsage

_MAX_RESULT_JSON = ((PDF_RAW_BYTES + 2) // 3) * 4 + 64 * 1024
_WORKER_SOCKET = "/run/trellis-egress/gateway.sock"
_POLICY = "public-http-v1"
_CONTROL_DEADLINE_SECONDS = 3.0


class WorkerBoundaryError(Exception):
    """The configured isolation boundary could not be proved or cleaned up."""


@dataclass(frozen=True, slots=True)
class WorkerDeployment:
    worker_image: str
    gateway_image: str
    gateway_container: str
    ipc_volume: str
    gateway_secret_file: Path

    def __post_init__(self) -> None:
        for image in (self.worker_image, self.gateway_image):
            if re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
                raise ValueError("retrieval images must be pinned local image IDs")
        for name in (self.gateway_container, self.ipc_volume):
            if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}", name) is None:
                raise ValueError("invalid retrieval deployment name")
        if not self.gateway_secret_file.is_absolute() or "\0" in str(self.gateway_secret_file):
            raise ValueError("gateway control secret requires an absolute file path")
        object.__setattr__(self, "gateway_secret_file", self.gateway_secret_file.resolve())


class LeaseControl(Protocol):
    async def is_ready(self) -> bool: ...

    async def issue(self, request: RetrievalRequest, timeout_seconds: float) -> str: ...

    async def revoke(self, token: str) -> GatewayReceipt | None: ...


@dataclass(frozen=True, slots=True)
class GatewayReceipt:
    tunnels: int
    transferred_bytes: int

    def __post_init__(self) -> None:
        if (
            type(self.tunnels) is not int
            or not 0 <= self.tunnels <= 300
            or type(self.transferred_bytes) is not int
            or not 0 <= self.transferred_bytes <= 64 * 1024 * 1024
        ):
            raise WorkerBoundaryError("invalid gateway usage receipt")


class GatewayControlClient:
    """A separate loopback-only client; its credential never enters the worker."""

    def __init__(self, base_url: str, admin_secret: str) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not parsed.port
        ):
            raise ValueError("gateway control requires a literal host-loopback URL")
        if re.fullmatch(r"[A-Za-z0-9_-]{32,256}", admin_secret) is None:
            raise ValueError("invalid gateway control credential")
        self._base_url = base_url.rstrip("/")
        self._secret = admin_secret

    async def _request(self, method: str, path: str, data: dict | None = None) -> dict:
        async with (
            asyncio.timeout(_CONTROL_DEADLINE_SECONDS),
            httpx.AsyncClient(trust_env=False, timeout=3.0) as client,
            client.stream(
                method,
                self._base_url + path,
                headers={"Authorization": "Bearer " + self._secret},
                json=data,
            ) as response,
        ):
            if response.status_code != (201 if method == "POST" else 200):
                raise WorkerBoundaryError("gateway control rejected the operation")
            payload = bytearray()
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise WorkerBoundaryError("encoded gateway control response")
            async for chunk in response.aiter_raw():
                payload.extend(chunk)
                if len(payload) > 4096:
                    raise WorkerBoundaryError("gateway control response exceeds limit")
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise WorkerBoundaryError("invalid gateway control response")
        return value

    async def is_ready(self) -> bool:
        try:
            return await self._request("GET", "/ready") == {"policy": _POLICY}
        except httpx.HTTPError, ValueError, WorkerBoundaryError, TimeoutError:
            return False

    async def issue(self, request: RetrievalRequest, timeout_seconds: float) -> str:
        value = await self._request(
            "POST",
            "/leases",
            {
                "ttl_seconds": min(request.timeout_seconds, timeout_seconds),
                "max_tunnels": request.max_requests,
                "max_bytes": request.max_response_bytes,
            },
        )
        token = value.get("token")
        if set(value) != {"token"} or not isinstance(token, str):
            raise WorkerBoundaryError("invalid gateway lease response")
        if re.fullmatch(r"[a-zA-Z0-9_-]{32,128}", token) is None:
            raise WorkerBoundaryError("invalid gateway lease token")
        return token

    async def revoke(self, token: str) -> GatewayReceipt | None:
        if re.fullmatch(r"[a-zA-Z0-9_-]{32,128}", token) is None:
            raise WorkerBoundaryError("invalid gateway lease token")
        value = await self._request("DELETE", "/leases/" + token)
        if set(value) != {"revoked", "receipt"} or value["revoked"] is not True:
            raise WorkerBoundaryError("gateway did not confirm socket revocation")
        receipt = value["receipt"]
        if receipt is None:
            return None
        if not isinstance(receipt, dict) or set(receipt) != {"tunnels", "transferred_bytes"}:
            raise WorkerBoundaryError("invalid gateway receipt")
        return GatewayReceipt(**receipt)


async def _read_bounded(reader: asyncio.StreamReader, limit: int) -> bytes:
    output = bytearray()
    while chunk := await reader.read(min(65536, limit + 1 - len(output))):
        output.extend(chunk)
        if len(output) > limit:
            raise WorkerBoundaryError("Docker operation output exceeds limit")
    return bytes(output)


async def _run_docker(
    args: tuple[str, ...],
    *,
    payload: bytes | None = None,
    limit: int = 65536,
    deadline_seconds: float = 5,
    capture_stderr: bool = False,
) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdin=asyncio.subprocess.PIPE if payload is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    try:
        async with asyncio.timeout(deadline_seconds):
            async with asyncio.TaskGroup() as tasks:
                output = tasks.create_task(_read_bounded(process.stdout, limit))
                errors = tasks.create_task(_read_bounded(process.stderr, 65536))
                if payload is not None:
                    assert process.stdin is not None
                    process.stdin.write(payload)
                    await process.stdin.drain()
                    process.stdin.close()
                code = await process.wait()
            return code, output.result() + (errors.result() if code or capture_stderr else b"")
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await process.wait()


def _worker_args(deployment: WorkerDeployment, name: str) -> tuple[str, ...]:
    return (
        "create",
        "--name",
        name,
        "--interactive",
        "--network",
        "none",
        "--ipc",
        "private",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "10001:10001",
        "--init",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--cpus",
        "1",
        "--pids-limit",
        "64",
        "--shm-size",
        "32m",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
        "--mount",
        f"type=volume,src={deployment.ipc_volume},dst=/run/trellis-egress,readonly",
        "--label",
        "trellis.role=retrieval-worker",
        deployment.worker_image,
        "python",
        "-m",
        "app.infrastructure.web_reader",
    )


def _inspect_worker(value: dict, deployment: WorkerDeployment) -> None:
    host, config = value.get("HostConfig", {}), value.get("Config", {})
    mounts = value.get("Mounts", [])
    expected_mounts = [
        mount
        for mount in mounts
        if mount.get("Type") == "volume"
        and mount.get("Name") == deployment.ipc_volume
        and mount.get("Destination") == "/run/trellis-egress"
        and mount.get("RW") is False
    ]
    if not (
        value.get("Image") == deployment.worker_image
        and config.get("User") == "10001:10001"
        and config.get("Cmd") == ["python", "-m", "app.infrastructure.web_reader"]
        and config.get("Entrypoint") in (None, [])
        and host.get("NetworkMode") == "none"
        and host.get("IpcMode") == "private"
        and host.get("ReadonlyRootfs") is True
        and host.get("Privileged") is False
        and host.get("CapDrop") in (["ALL"], ["CAP_ALL"])
        and host.get("CapAdd") in (None, [])
        and host.get("SecurityOpt") == ["no-new-privileges"]
        and host.get("Init") is True
        and host.get("Memory") == 512 * 1024 * 1024
        and host.get("MemorySwap") == 512 * 1024 * 1024
        and host.get("NanoCpus") == 1_000_000_000
        and host.get("PidsLimit") == 64
        and host.get("ShmSize") == 32 * 1024 * 1024
        and host.get("Tmpfs") == {"/tmp": "rw,noexec,nosuid,nodev,size=64m,mode=1777"}
        and len(mounts) == len(expected_mounts) == 1
        and not host.get("Devices")
        and not host.get("DeviceRequests")
        and not host.get("PidMode")
        and not host.get("PortBindings")
    ):
        raise WorkerBoundaryError("actual worker isolation differs from the required policy")


def _decode_result(payload: bytes, request: RetrievalRequest) -> RetrievalResult:
    try:
        value = json.loads(payload)
        if not isinstance(value, dict) or set(value) != {
            "status",
            "requested_url",
            "final_url",
            "media_type",
            "body",
            "usage",
            "error_code",
        }:
            raise ValueError("invalid result fields")
        if value["requested_url"] != request.url:
            raise ValueError("mismatched retrieval request")
        usage = WebUsage.from_dict(value["usage"])
        if (
            usage.search_calls
            or usage.extracted_bytes
            or usage.page_attempts != 1
            or usage.network_requests > request.max_requests
            or usage.response_bytes > request.max_response_bytes
        ):
            raise ValueError("retrieval result exceeds its allowance")
        body = base64.b64decode(value["body"], validate=True)
        if value["media_type"] in {"text/html", "text/plain"} and len(body) > HTML_DECODED_BYTES:
            raise ValueError("HTML exceeds decoded limit")
        for key in ("final_url", "media_type", "error_code"):
            if value[key] is not None and (
                not isinstance(value[key], str) or len(value[key]) > 4096
            ):
                raise ValueError("invalid retrieval result metadata")
        return RetrievalResult(
            RetrievalStatus(value["status"]),
            request.url,
            value["final_url"],
            value["media_type"],
            body,
            usage,
            value["error_code"],
        )
    except ValueError, TypeError, KeyError, binascii.Error:
        raise WorkerBoundaryError("invalid bounded worker result") from None


class DockerPublicRetriever:
    def __init__(self, deployment: WorkerDeployment, control: LeaseControl) -> None:
        self._deployment = deployment
        self._control = control

    async def _inspect(self, name: str) -> dict:
        code, payload = await _run_docker(("inspect", name))
        if code:
            raise WorkerBoundaryError("could not inspect retrieval container")
        value = json.loads(payload)
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise WorkerBoundaryError("invalid retrieval container inspection")
        return value[0]

    async def is_ready(self) -> bool:
        try:
            gateway = await self._inspect(self._deployment.gateway_container)
            host = gateway.get("HostConfig", {})
            ports = gateway.get("NetworkSettings", {}).get("Ports", {})
            mounts = gateway.get("Mounts", [])
            binding = ports.get("8081/tcp")
            if (
                not isinstance(binding, list)
                or len(binding) != 1
                or not isinstance(binding[0], dict)
                or set(binding[0]) != {"HostIp", "HostPort"}
                or binding[0]["HostIp"] != "127.0.0.1"
                or not isinstance(binding[0]["HostPort"], str)
                or not binding[0]["HostPort"].isdigit()
                or not 1 <= int(binding[0]["HostPort"]) <= 65535
                or set(ports) != {"8081/tcp"}
            ):
                return False
            if not (
                gateway.get("Image") == self._deployment.gateway_image
                and gateway.get("State", {}).get("Running") is True
                and host.get("Privileged") is False
                and host.get("ReadonlyRootfs") is True
                and host.get("CapDrop") in (["ALL"], ["CAP_ALL"])
                and {cap.removeprefix("CAP_") for cap in host.get("CapAdd") or []}
                == {"NET_ADMIN", "CHOWN", "SETUID", "SETGID", "SETPCAP"}
                and host.get("Memory") == 256 * 1024 * 1024
                and host.get("MemorySwap") == 256 * 1024 * 1024
                and host.get("NanoCpus") == 1_000_000_000
                and host.get("PidsLimit") == 64
                and host.get("ShmSize") == 16 * 1024 * 1024
                and host.get("IpcMode") == "private"
                and host.get("Tmpfs") == {"/tmp": "rw,noexec,nosuid,nodev,size=16m,mode=1777"}
                and not host.get("Devices")
                and not host.get("DeviceRequests")
                and not host.get("PidMode")
                and host.get("SecurityOpt") == ["no-new-privileges"]
                and len(mounts) == 2
                and any(
                    m.get("Name") == self._deployment.ipc_volume
                    and m.get("Destination") == "/run/trellis-egress"
                    and m.get("RW") is True
                    for m in mounts
                )
                and any(
                    m.get("Type") == "bind"
                    and m.get("Source") in self._secret_mount_sources()
                    and m.get("Destination") == "/run/secrets/gateway-admin"
                    and m.get("RW") is False
                    for m in mounts
                )
            ):
                return False
            code, output = await _run_docker(
                (
                    "exec",
                    "--user",
                    "0",
                    self._deployment.gateway_container,
                    "/usr/local/bin/trellis-check-egress-policy",
                )
            )
            return (
                code == 0 and output.strip() == _POLICY.encode() and await self._control.is_ready()
            )
        except OSError, ValueError, WorkerBoundaryError, TimeoutError, ExceptionGroup:
            return False

    def _secret_mount_sources(self) -> frozenset[str]:
        source = str(self._deployment.gateway_secret_file)
        return frozenset({source, "/host_mnt" + source} if sys.platform == "darwin" else {source})

    async def _cleanup(
        self, name: str, token: str | None, creation_attempted: bool
    ) -> GatewayReceipt | None:
        failures = []
        receipt = None
        if token is not None:
            try:
                receipt = await self._control.revoke(token)
            except Exception as error:
                failures.append(error)
        if creation_attempted:
            code, _ = await _run_docker(("rm", "--force", name), deadline_seconds=10)
            if code:
                check_code, output = await _run_docker(("inspect", name))
                if check_code == 0 or not any(
                    text in output for text in (b"No such object", b"No such container")
                ):
                    failures.append(
                        WorkerBoundaryError("retrieval container removal is unconfirmed")
                    )
        if failures:
            raise WorkerBoundaryError("retrieval cleanup could not be confirmed")
        return receipt

    async def retrieve(
        self, request: RetrievalRequest, context: ToolExecutionContext
    ) -> RetrievalResult:
        await context.assert_active()
        timeout = min(request.timeout_seconds, context.remaining_seconds)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        name = "trellis-retrieval-" + uuid4().hex
        token: str | None = None
        creation_attempted = False
        try:
            async with asyncio.timeout_at(deadline):
                if not await self.is_ready():
                    raise WorkerBoundaryError("retrieval boundary is not ready")
                await context.assert_active()
                remaining = min(context.remaining_seconds, deadline - loop.time())
                if remaining <= 0:
                    raise TimeoutError
                token = await self._control.issue(request, remaining)
                creation_attempted = True
                code, _ = await _run_docker(_worker_args(self._deployment, name))
                if code:
                    raise WorkerBoundaryError("could not create retrieval worker")
                _inspect_worker(await self._inspect(name), self._deployment)
                await context.assert_active()
                remaining = min(context.remaining_seconds, deadline - loop.time())
                if remaining <= 0:
                    raise TimeoutError
                payload = (
                    json.dumps(
                        {
                            **asdict(request),
                            "timeout_seconds": remaining,
                            "gateway_socket": _WORKER_SOCKET,
                            "lease_token": token,
                        },
                        ensure_ascii=False,
                    ).encode()
                    + b"\n"
                )
                if len(payload) > MAX_WORKER_REQUEST_BYTES:
                    raise WorkerBoundaryError("retrieval worker request exceeds limit")
                code, output = await _run_docker(
                    ("start", "--attach", "--interactive", name),
                    payload=payload,
                    limit=_MAX_RESULT_JSON,
                    deadline_seconds=remaining,
                )
                if code:
                    raise WorkerBoundaryError("retrieval worker did not finish")
                result = _decode_result(output, request)
                await context.assert_active()
        except TimeoutError:
            result = RetrievalResult(
                RetrievalStatus.TIMEOUT,
                request.url,
                error_code="worker_timeout",
            )
        except OSError, ValueError, WorkerBoundaryError, httpx.HTTPError, ExceptionGroup:
            result = RetrievalResult(
                RetrievalStatus.UNAVAILABLE,
                request.url,
                error_code="worker_unavailable",
            )
        finally:
            cleanup = asyncio.create_task(self._cleanup(name, token, creation_attempted))
            interrupted = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    interrupted = True
                    continue
            receipt = cleanup.result()
            if interrupted:
                raise asyncio.CancelledError
        if (
            receipt is not None
            and receipt.transferred_bytes == request.max_response_bytes
            and result.status in {RetrievalStatus.UNAVAILABLE, RetrievalStatus.TIMEOUT}
        ):
            result = replace(
                result,
                status=RetrievalStatus.BUDGET_EXHAUSTED,
                error_code="gateway_transport_allowance_exhausted",
            )
        return replace(result, usage=self._settled_usage(request, receipt, token is not None))

    @staticmethod
    def _settled_usage(
        request: RetrievalRequest, receipt: GatewayReceipt | None, issued: bool
    ) -> WebUsage:
        if receipt is not None:
            if (
                not isinstance(receipt, GatewayReceipt)
                or receipt.tunnels > request.max_requests
                or receipt.transferred_bytes > request.max_response_bytes
            ):
                raise WorkerBoundaryError("gateway receipt exceeds its reservation")
            return WebUsage(
                page_attempts=1,
                network_requests=receipt.tunnels,
                response_bytes=receipt.transferred_bytes,
            )
        return WebUsage(
            page_attempts=1,
            network_requests=request.max_requests if issued else 0,
            response_bytes=request.max_response_bytes if issued else 0,
        )
