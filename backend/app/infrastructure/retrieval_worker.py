"""Fixed, inspected Docker operations for untrusted public document processing."""

import asyncio
import base64
import binascii
import hashlib
import json
import re
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from app.application.tool_context import ToolExecutionContext
from app.domain.retrieval import (
    HTML_DECODED_BYTES,
    MAX_EXTRACTED_RESULT_BYTES,
    MAX_WORKER_REQUEST_BYTES,
    PDF_RAW_BYTES,
    ExtractedDocumentResult,
    RenderRequest,
    RetrievalRequest,
    RetrievalResult,
    RetrievalStatus,
)
from app.domain.web import SourceStatus, WebUsage

_MAX_RESULT_JSON = ((PDF_RAW_BYTES + 2) // 3) * 4 + 64 * 1024
_WORKER_SOCKET = "/run/trellis-egress/gateway.sock"
_POLICY = "public-http-v1"
_CONTROL_DEADLINE_SECONDS = 3.0
_BROWSER_PROFILE = Path(__file__).resolve().parents[2] / "containers/chromium-seccomp.json"
_BROWSER_PROFILE_SHA256 = "9c86a46bf3b2ef2c7cfece632b4ece1127698810857ef98914f87d99fdc74a27"


class WorkerBoundaryError(Exception):
    """The configured isolation boundary could not be proved or cleaned up."""


class RetrievalConcurrency:
    """One backend owns two HTTP slots and one renderer, including readiness probes."""

    def __init__(self) -> None:
        self._http = asyncio.Semaphore(2)
        self._render = asyncio.Lock()
        self.proved_renderers: set[tuple[str, str]] = set()

    @asynccontextmanager
    async def operation(self, *, render: bool) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            if render:
                await stack.enter_async_context(self._render)
            await stack.enter_async_context(self._http)
            if render:
                await stack.enter_async_context(self._http)
            yield


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

    async def is_render_ready(self) -> bool: ...

    async def issue(
        self, request: RetrievalRequest | RenderRequest, timeout_seconds: float
    ) -> str: ...

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

    async def is_render_ready(self) -> bool:
        try:
            return await self._request("GET", "/ready/render") == {"policy": "public-render-v1"}
        except httpx.HTTPError, ValueError, WorkerBoundaryError, TimeoutError:
            return False

    async def issue(self, request: RetrievalRequest | RenderRequest, timeout_seconds: float) -> str:
        value = await self._request(
            "POST",
            "/leases",
            {
                "ttl_seconds": min(request.timeout_seconds, timeout_seconds),
                "max_tunnels": request.max_requests,
                "max_bytes": request.max_response_bytes,
                **({"render": True} if isinstance(request, RenderRequest) else {}),
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


def _worker_args(
    deployment: WorkerDeployment, name: str, *, render: bool = False
) -> tuple[str, ...]:
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
        *(("--security-opt", "seccomp=" + str(_BROWSER_PROFILE)) if render else ()),
        "--user",
        "10001:10001",
        "--init",
        "--memory",
        "2g" if render else "512m",
        "--memory-swap",
        "2g" if render else "512m",
        "--cpus",
        "2" if render else "1",
        "--pids-limit",
        "256" if render else "64",
        "--shm-size",
        "256m" if render else "32m",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777"
        if render
        else "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
        "--mount",
        f"type=volume,src={deployment.ipc_volume},dst=/run/trellis-egress,readonly",
        "--label",
        "trellis.role=retrieval-worker",
        deployment.worker_image,
        "python",
        "-m",
        "app.infrastructure.web_renderer" if render else "app.infrastructure.web_reader",
    )


def _inspect_worker(value: dict, deployment: WorkerDeployment, *, render: bool = False) -> None:
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
        and config.get("Cmd")
        == [
            "python",
            "-m",
            "app.infrastructure.web_renderer" if render else "app.infrastructure.web_reader",
        ]
        and config.get("Entrypoint") in (None, [])
        and host.get("NetworkMode") == "none"
        and host.get("IpcMode") == "private"
        and host.get("ReadonlyRootfs") is True
        and host.get("Privileged") is False
        and host.get("CapDrop") in (["ALL"], ["CAP_ALL"])
        and host.get("CapAdd") in (None, [])
        and (
            _browser_security(host.get("SecurityOpt"))
            if render
            else host.get("SecurityOpt") == ["no-new-privileges"]
        )
        and host.get("Init") is True
        and host.get("Memory") == (2048 if render else 512) * 1024 * 1024
        and host.get("MemorySwap") == (2048 if render else 512) * 1024 * 1024
        and host.get("NanoCpus") == (2 if render else 1) * 1_000_000_000
        and host.get("PidsLimit") == (256 if render else 64)
        and host.get("ShmSize") == (256 if render else 32) * 1024 * 1024
        and host.get("Tmpfs")
        == {
            "/tmp": "rw,noexec,nosuid,nodev,size=256m,mode=1777"
            if render
            else "rw,noexec,nosuid,nodev,size=64m,mode=1777"
        }
        and len(mounts) == len(expected_mounts) == 1
        and not host.get("Devices")
        and not host.get("DeviceRequests")
        and not host.get("PidMode")
        and not host.get("PortBindings")
    ):
        raise WorkerBoundaryError("actual worker isolation differs from the required policy")


def _browser_security(options: object) -> bool:
    if not isinstance(options, list) or len(options) != 2 or "no-new-privileges" not in options:
        return False
    try:
        profile = next(
            item.removeprefix("seccomp=") for item in options if item.startswith("seccomp=")
        )
        return json.loads(profile) == json.loads(_BROWSER_PROFILE.read_text())
    except StopIteration, ValueError, OSError, AttributeError:
        return False


def _decode_result(payload: bytes, request: RetrievalRequest | RenderRequest) -> RetrievalResult:
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


def _decode_document(
    payload: bytes, request: RetrievalRequest | RenderRequest
) -> ExtractedDocumentResult:
    try:
        if len(payload) > MAX_EXTRACTED_RESULT_BYTES:
            raise ValueError("extracted result exceeds limit")
        value = json.loads(payload)
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "status",
                "requested_url",
                "final_url",
                "media_type",
                "text",
                "usage",
                "error_code",
                "title",
                "publisher",
                "published_at",
                "canonical_url",
                "links",
                "extractor_version",
            }
            or value["requested_url"] != request.url
            or not isinstance(value["links"], list)
        ):
            raise ValueError("invalid extracted result fields")
        usage = WebUsage.from_dict(value["usage"])
        if (
            usage.search_calls
            or usage.page_attempts != 1
            or usage.network_requests > request.max_requests
            or usage.response_bytes > request.max_response_bytes
        ):
            raise ValueError("extracted result exceeds its allowance")
        return ExtractedDocumentResult(
            status=SourceStatus(value["status"]),
            requested_url=value["requested_url"],
            final_url=value["final_url"],
            media_type=value["media_type"],
            text=value["text"],
            usage=usage,
            error_code=value["error_code"],
            title=value["title"],
            publisher=value["publisher"],
            published_at=value["published_at"],
            canonical_url=value["canonical_url"],
            links=tuple(value["links"]),
            extractor_version=value["extractor_version"],
        )
    except ValueError, TypeError, KeyError:
        raise WorkerBoundaryError("invalid bounded extracted worker result") from None


class DockerPublicRetriever:
    def __init__(
        self,
        deployment: WorkerDeployment,
        control: LeaseControl,
        *,
        concurrency: RetrievalConcurrency | None = None,
    ) -> None:
        self._deployment = deployment
        self._control = control
        self._concurrency = concurrency or RetrievalConcurrency()

    async def is_crawl_ready(self) -> bool:
        try:
            async with asyncio.timeout(15):
                if not await self.is_ready():
                    return False
                code, payload = await _run_docker(
                    ("image", "inspect", self._deployment.worker_image)
                )
                images = json.loads(payload) if code == 0 else None
                if (
                    not isinstance(images, list)
                    or len(images) != 1
                    or not isinstance(images[0], dict)
                ):
                    return False
                config = images[0].get("Config")
                labels = config.get("Labels") if isinstance(config, dict) else None
                return (
                    isinstance(labels, dict)
                    and labels.get("trellis.crawl.protocol") == "public-crawl-v1"
                )
        except OSError, ValueError, WorkerBoundaryError, TimeoutError, ExceptionGroup:
            return False

    async def is_render_ready(self) -> bool:
        try:
            async with asyncio.timeout(30):
                if not await self.is_ready():
                    return False
                if await self._control.is_render_ready() is not True:
                    return False
                digest = hashlib.sha256(_BROWSER_PROFILE.read_bytes()).hexdigest()
                if digest != _BROWSER_PROFILE_SHA256:
                    return False
                code, payload = await _run_docker(
                    ("image", "inspect", self._deployment.worker_image)
                )
                images = json.loads(payload) if code == 0 else []
                labels = images[0].get("Config", {}).get("Labels", {}) if len(images) == 1 else {}
                if (
                    labels.get("trellis.browser.protocol") != "public-render-v1"
                    or labels.get("trellis.browser.seccomp-sha256") != digest
                ):
                    return False
                key = (self._deployment.worker_image, digest)
                async with self._concurrency.operation(render=True):
                    if key in self._concurrency.proved_renderers:
                        return True
                    name = "trellis-browser-probe-" + uuid4().hex
                    try:
                        code, _ = await _run_docker(
                            _worker_args(self._deployment, name, render=True)
                        )
                        if code:
                            return False
                        _inspect_worker(await self._inspect(name), self._deployment, render=True)
                        code, payload = await _run_docker(
                            ("start", "--attach", "--interactive", name),
                            payload=b'{"operation":"sandbox_probe"}\n',
                            limit=4096,
                            deadline_seconds=20,
                        )
                        proved = code == 0 and json.loads(payload) == {
                            "protocol": "public-render-v1",
                            "sandbox": True,
                        }
                    finally:
                        await self._cleanup_safely(name, None, True)
                    if proved:
                        self._concurrency.proved_renderers.add(key)
                    return proved
        except OSError, ValueError, IndexError, WorkerBoundaryError, TimeoutError, ExceptionGroup:
            return False

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
            code, payload = await _run_docker(("image", "inspect", self._deployment.worker_image))
            images = json.loads(payload) if code == 0 else None
            if (
                not isinstance(images, list)
                or len(images) != 1
                or not isinstance(images[0], dict)
                or images[0].get("Id") != self._deployment.worker_image
                or images[0].get("Os") != "linux"
            ):
                return False
            image_config = images[0].get("Config")
            labels = image_config.get("Labels") if isinstance(image_config, dict) else None
            if (
                not isinstance(labels, dict)
                or labels.get("trellis.worker.protocol") != "public-document-v1"
            ):
                return False
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

    async def _cleanup_safely(
        self, name: str, token: str | None, creation_attempted: bool
    ) -> GatewayReceipt | None:
        cleanup = asyncio.create_task(self._cleanup(name, token, creation_attempted))
        interrupted = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
        receipt = cleanup.result()
        if interrupted:
            raise asyncio.CancelledError
        return receipt

    async def retrieve(
        self, request: RetrievalRequest, context: ToolExecutionContext
    ) -> RetrievalResult:
        result = await self._retrieve(request, context, extracted=False)
        assert isinstance(result, RetrievalResult)
        return result

    async def read_document(
        self, request: RetrievalRequest, context: ToolExecutionContext
    ) -> ExtractedDocumentResult:
        result = await self._retrieve(request, context, extracted=True)
        assert isinstance(result, ExtractedDocumentResult)
        return result

    async def render_document(
        self, request: RenderRequest, context: ToolExecutionContext
    ) -> ExtractedDocumentResult:
        result = await self._retrieve(request, context, extracted=True, render=True)
        assert isinstance(result, ExtractedDocumentResult)
        return result

    async def crawl_document(
        self, request: RetrievalRequest, context: ToolExecutionContext
    ) -> ExtractedDocumentResult:
        if request.scope_origin is None:
            raise ValueError("crawl retrieval requires a bounded scope")
        result = await self._retrieve(request, context, extracted=True, crawl=True)
        assert isinstance(result, ExtractedDocumentResult)
        return result

    async def _retrieve(
        self,
        request: RetrievalRequest | RenderRequest,
        context: ToolExecutionContext,
        *,
        extracted: bool,
        render: bool = False,
        crawl: bool = False,
    ) -> RetrievalResult | ExtractedDocumentResult:
        await context.assert_active()
        timeout = min(request.timeout_seconds, context.remaining_seconds)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        name = "trellis-retrieval-" + uuid4().hex
        token: str | None = None
        creation_attempted = False
        operation = AsyncExitStack()
        try:
            async with asyncio.timeout_at(deadline):
                if not await (
                    self.is_render_ready()
                    if render
                    else self.is_crawl_ready()
                    if crawl
                    else self.is_ready()
                ):
                    raise WorkerBoundaryError("retrieval boundary is not ready")
                await operation.enter_async_context(self._concurrency.operation(render=render))
                await context.assert_active()
                remaining = min(context.remaining_seconds, deadline - loop.time())
                if remaining <= 0:
                    raise TimeoutError
                token = await self._control.issue(request, remaining)
                creation_attempted = True
                code, _ = await _run_docker(_worker_args(self._deployment, name, render=render))
                if code:
                    raise WorkerBoundaryError("could not create retrieval worker")
                _inspect_worker(await self._inspect(name), self._deployment, render=render)
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
                            "operation": "render"
                            if render
                            else "crawl"
                            if crawl
                            else "extracted"
                            if extracted
                            else "raw",
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
                    limit=MAX_EXTRACTED_RESULT_BYTES if extracted else _MAX_RESULT_JSON,
                    deadline_seconds=remaining,
                )
                if code:
                    raise WorkerBoundaryError("retrieval worker did not finish")
                result = (
                    _decode_document(output, request)
                    if extracted
                    else _decode_result(output, request)
                )
                await context.assert_active()
        except TimeoutError:
            result = (
                ExtractedDocumentResult(
                    SourceStatus.TIMEOUT, request.url, error_code="worker_timeout"
                )
                if extracted
                else RetrievalResult(
                    RetrievalStatus.TIMEOUT, request.url, error_code="worker_timeout"
                )
            )
        except OSError, ValueError, WorkerBoundaryError, httpx.HTTPError, ExceptionGroup:
            result = (
                ExtractedDocumentResult(
                    SourceStatus.UNAVAILABLE, request.url, error_code="worker_unavailable"
                )
                if extracted
                else RetrievalResult(
                    RetrievalStatus.UNAVAILABLE, request.url, error_code="worker_unavailable"
                )
            )
        finally:
            try:
                receipt = await self._cleanup_safely(name, token, creation_attempted)
            finally:
                await operation.aclose()
        if (
            receipt is not None
            and receipt.transferred_bytes == request.max_response_bytes
            and result.status.value in {"unavailable", "timeout"}
        ):
            result = replace(
                result,
                status=SourceStatus.BUDGET_EXHAUSTED
                if extracted
                else RetrievalStatus.BUDGET_EXHAUSTED,
                error_code="gateway_transport_allowance_exhausted",
            )
        usage = self._settled_usage(request, receipt, token is not None)
        if render and receipt is not None:
            usage = replace(
                usage, network_requests=max(usage.network_requests, result.usage.network_requests)
            )
        return replace(result, usage=replace(usage, extracted_bytes=result.usage.extracted_bytes))

    @staticmethod
    def _settled_usage(
        request: RetrievalRequest | RenderRequest, receipt: GatewayReceipt | None, issued: bool
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
