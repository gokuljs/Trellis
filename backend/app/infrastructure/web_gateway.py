"""Trusted DNS-pinning gateway. Workers receive expiring data-plane leases only."""

import argparse
import asyncio
import hmac
import json
import os
import re
import secrets
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.domain.retrieval import MAX_NETWORK_REQUESTS, MAX_RESPONSE_BYTES, MAX_RETRIEVAL_SECONDS
from app.infrastructure.web_network import DestinationBlocked, PublicConnector

POLICY_VERSION = "public-http-v1"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


class Connector(Protocol):
    async def connect(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]: ...


@dataclass(slots=True)
class _Lease:
    expires_at: float
    max_tunnels: int
    max_bytes: int
    tunnels: int = 0
    transferred: int = 0
    writers: set[asyncio.StreamWriter] = field(default_factory=set)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    expiry: asyncio.TimerHandle | None = None


class PublicGateway:
    def __init__(self, admin_secret: str, connector: Connector | None = None) -> None:
        if (
            len(admin_secret) < 32
            or not admin_secret.isascii()
            or any(char.isspace() for char in admin_secret)
        ):
            raise ValueError("gateway admin secret requires at least 32 non-space ASCII characters")
        self._admin_secret = admin_secret
        self._connector = connector or PublicConnector()
        self._leases: dict[str, _Lease] = {}
        self._receipts: dict[str, dict[str, int]] = {}
        self._handlers: set[asyncio.Task[None]] = set()

    def issue(self, ttl_seconds: float, max_tunnels: int, max_bytes: int) -> str:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int | float)
            or not 0 < ttl_seconds <= MAX_RETRIEVAL_SECONDS
            or type(max_tunnels) is not int
            or not 1 <= max_tunnels <= MAX_NETWORK_REQUESTS
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= MAX_RESPONSE_BYTES
        ):
            raise ValueError("invalid lease allowance")
        if len(self._leases) >= 256:
            raise ValueError("gateway lease capacity exhausted")
        token = secrets.token_urlsafe(32)
        loop = asyncio.get_running_loop()
        lease = _Lease(loop.time() + ttl_seconds, max_tunnels, max_bytes)
        self._leases[token] = lease
        lease.expiry = loop.call_later(ttl_seconds, self._expire, token)
        return token

    def _expire(self, token: str) -> _Lease | None:
        lease = self._leases.pop(token, None)
        if lease is not None:
            if lease.expiry:
                lease.expiry.cancel()
            for writer in lease.writers:
                writer.close()
            for task in lease.tasks:
                task.cancel()
            self._receipts[token] = {
                "tunnels": lease.tunnels,
                "transferred_bytes": lease.transferred,
            }
            while len(self._receipts) > 512:
                self._receipts.pop(next(iter(self._receipts)))
        return lease

    async def revoke(self, token: str) -> dict[str, int] | None:
        lease = self._expire(token)
        if lease:
            await asyncio.gather(
                *(self._closed(writer) for writer in tuple(lease.writers)),
                *tuple(lease.tasks),
                return_exceptions=True,
            )
        return self._receipts.get(token)

    async def close(self) -> None:
        await asyncio.gather(*(self.revoke(token) for token in tuple(self._leases)))
        handlers = tuple(self._handlers)
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)

    @staticmethod
    async def _closed(writer: asyncio.StreamWriter) -> None:
        writer.close()
        try:
            async with asyncio.timeout(1):
                await writer.wait_closed()
        except OSError, TimeoutError:
            pass

    async def _reply(self, writer: asyncio.StreamWriter, value: dict[str, object]) -> None:
        writer.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")
        await writer.drain()

    async def handle_tunnel(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        remote: asyncio.StreamWriter | None = None
        lease: _Lease | None = None
        pumps: list[asyncio.Task[None]] = []
        task = asyncio.current_task()
        assert task is not None
        if len(self._handlers) >= 256:
            await self._closed(writer)
            return
        self._handlers.add(task)
        try:
            async with asyncio.timeout(3):
                raw = await reader.readline()
            if len(raw) > 8192:
                raise ValueError("oversized tunnel request")
            message = json.loads(raw)
            if not isinstance(message, dict) or set(message) != {"lease_token", "host", "port"}:
                raise ValueError("invalid tunnel request")
            token, host, port = message["lease_token"], message["host"], message["port"]
            if (
                not isinstance(token, str)
                or not _TOKEN.fullmatch(token)
                or not isinstance(host, str)
                or not 1 <= len(host) <= 253
                or type(port) is not int
            ):
                raise ValueError("invalid tunnel fields")
            lease = self._leases.get(token)
            if lease is None or lease.expires_at <= asyncio.get_running_loop().time():
                await self._reply(writer, {"status": "timeout", "error_code": "lease_expired"})
                return
            if lease.tunnels >= lease.max_tunnels or lease.transferred >= lease.max_bytes:
                await self._reply(
                    writer, {"status": "budget_exhausted", "error_code": "lease_exhausted"}
                )
                return
            lease.tunnels += 1
            lease.writers.add(writer)
            lease.tasks.add(task)
            async with asyncio.timeout_at(lease.expires_at):
                remote_reader, remote = await self._connector.connect(host, port)
                lease.writers.add(remote)
                if self._leases.get(token) is not lease:
                    return
                await self._reply(writer, {"status": "ok"})

                async def pump(source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
                    while chunk := await source.read(
                        min(16384, max(0, lease.max_bytes - lease.transferred))
                    ):
                        if (
                            self._leases.get(token) is not lease
                            or lease.expires_at <= asyncio.get_running_loop().time()
                        ):
                            return
                        available = lease.max_bytes - lease.transferred
                        forwarded = chunk[:available]
                        lease.transferred += len(forwarded)
                        target.write(forwarded)
                        await target.drain()
                        if len(forwarded) != len(chunk) or lease.transferred >= lease.max_bytes:
                            self._expire(token)
                            return

                pumps = [
                    asyncio.create_task(pump(reader, remote)),
                    asyncio.create_task(pump(remote_reader, writer)),
                ]
                await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        except DestinationBlocked:
            await self._reply(writer, {"status": "blocked", "error_code": "destination_blocked"})
        except TimeoutError:
            if remote is None and not writer.is_closing():
                await self._reply(writer, {"status": "timeout", "error_code": "gateway_timeout"})
        except OSError, ValueError, KeyError, TypeError:
            if remote is None and not writer.is_closing():
                with suppress(OSError):
                    await self._reply(
                        writer, {"status": "unavailable", "error_code": "gateway_unavailable"}
                    )
        finally:
            for pump_task in pumps:
                pump_task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            await self._closed(writer)
            if remote:
                await self._closed(remote)
            if lease:
                lease.tasks.discard(task)
                lease.writers.discard(writer)
                if remote:
                    lease.writers.discard(remote)
            self._handlers.discard(task)

    async def handle_admin(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        status = 400
        result: Mapping[str, object] = {"error": "invalid_request"}
        try:
            async with asyncio.timeout(3):
                raw_headers = await reader.readuntil(b"\r\n\r\n")
                if len(raw_headers) > 8192:
                    raise ValueError("oversized headers")
                lines = raw_headers.decode("ascii").split("\r\n")
                method, path, version = lines[0].split(" ")
                headers: dict[str, str] = {}
                for line in lines[1:-2]:
                    key, value = line.split(":", 1)
                    key = key.lower()
                    if key in headers:
                        raise ValueError("duplicate header")
                    headers[key] = value.strip()
                length = int(headers.get("content-length", "0"))
                if (
                    version != "HTTP/1.1"
                    or not 0 <= length <= 4096
                    or "transfer-encoding" in headers
                ):
                    raise ValueError("invalid framing")
                authorized = hmac.compare_digest(
                    headers.get("authorization", ""), f"Bearer {self._admin_secret}"
                )
                if not authorized:
                    status, result = 403, {"error": "access_denied"}
                elif method == "GET" and path == "/ready" and length == 0:
                    status, result = 200, {"policy": POLICY_VERSION}
                elif method == "POST" and path == "/leases":
                    payload = json.loads(await reader.readexactly(length))
                    if not isinstance(payload, dict) or set(payload) != {
                        "ttl_seconds",
                        "max_tunnels",
                        "max_bytes",
                    }:
                        raise ValueError("invalid lease fields")
                    token = self.issue(**payload)
                    status, result = 201, {"token": token}
                elif method == "DELETE" and path.startswith("/leases/") and length == 0:
                    token = path.removeprefix("/leases/")
                    if not _TOKEN.fullmatch(token):
                        raise ValueError("invalid token")
                    receipt = await self.revoke(token)
                    status = 200
                    result = {"revoked": True, "receipt": receipt}
                else:
                    status, result = 404, {"error": "not_found"}
        except (
            OSError,
            ValueError,
            UnicodeError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            pass
        body = json.dumps(result, separators=(",", ":")).encode()
        try:
            writer.write(
                f"HTTP/1.1 {status} Result\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except OSError:
            pass
        finally:
            await self._closed(writer)


async def _serve(socket_path: Path, admin_host: str, admin_port: int, secret: str) -> None:
    gateway = PublicGateway(secret)
    tunnel = await asyncio.start_unix_server(gateway.handle_tunnel, path=socket_path, limit=16384)
    os.chmod(socket_path, 0o666)
    admin = await asyncio.start_server(gateway.handle_admin, admin_host, admin_port, limit=16384)
    try:
        await asyncio.gather(tunnel.serve_forever(), admin.serve_forever())
    finally:
        tunnel.close()
        admin.close()
        await gateway.close()
        await asyncio.gather(tunnel.wait_closed(), admin.wait_closed())
        await asyncio.to_thread(socket_path.unlink, missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--admin-host", default="0.0.0.0")
    parser.add_argument("--admin-port", type=int, default=8081)
    parser.add_argument("--admin-secret-file", type=Path, required=True)
    args = parser.parse_args()
    secret = args.admin_secret_file.read_text().strip()
    args.socket.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    args.socket.unlink(missing_ok=True)
    asyncio.run(_serve(args.socket, args.admin_host, args.admin_port, secret))


if __name__ == "__main__":
    main()
