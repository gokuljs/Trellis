"""Build and exercise the real Docker boundary against a deterministic TLS fixture.

Run from backend with `uv run python -m scripts.verify_web_isolation`.
The Docker daemon must support Linux containers, iptables, and named volumes.
Run one proof per Docker daemon. The fixture reserves 93.184.216.0/24.
All test containers, networks, volumes, secrets, and derived images are removed.
Production worker/gateway images remain available by the printed immutable IDs.
"""

import asyncio
import json
import logging
import os
import secrets
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from app.application.budgets import RunLimits
from app.application.tool_context import ToolExecutionContext
from app.domain.retrieval import RetrievalRequest, RetrievalStatus
from app.infrastructure.retrieval_worker import (
    DockerPublicRetriever,
    GatewayControlClient,
    WorkerDeployment,
    _run_docker,
    _worker_args,
)

_BACKEND = Path(__file__).resolve().parents[1]
_FIXTURE_IP = "93.184.216.34"
_GATEWAY = """import asyncio
import os
from pathlib import Path
from app.infrastructure._production_gateway import PublicGateway
from app.infrastructure.web_network import PublicConnector

class Resolver:
    def __init__(self):
        self.rebindings = 0
    async def resolve(self, host, port):
        if host == "mixed.test":
            return ("93.184.216.34", "127.0.0.1")
        if host == "private.test":
            return ("10.0.0.1",)
        if host == "metadata.test":
            return ("169.254.169.254",)
        if host == "mapped.test":
            return ("::ffff:93.184.216.34",)
        if host == "ipv6.test":
            return ("fd00::1",)
        if host == "rebind.test":
            self.rebindings += 1
            return ("93.184.216.34",) if self.rebindings == 1 else ("127.0.0.1",)
        return ("93.184.216.34",)

async def main():
    secret = Path("/run/secrets/gateway-admin").read_text().strip()
    gateway = PublicGateway(secret, PublicConnector(Resolver()))
    path = "/run/trellis-egress/gateway.sock"
    Path(path).unlink(missing_ok=True)
    unix = await asyncio.start_unix_server(gateway.handle_tunnel, path=path, limit=16384)
    os.chmod(path, 0o666)
    admin = await asyncio.start_server(gateway.handle_admin, "0.0.0.0", 8081, limit=16384)
    async with unix, admin:
        await asyncio.gather(unix.serve_forever(), admin.serve_forever())


if __name__ == "__main__":
    asyncio.run(main())
"""
_FIXTURE = """import asyncio
import gzip
import json
import ssl

async def handle(reader, writer):
    try:
        headers = await reader.readuntil(b"\\r\\n\\r\\n")
        path = headers.split(b" ")[1].decode()
        host = next(
            line.split(b":", 1)[1].strip().decode()
            for line in headers.split(b"\\r\\n")
            if line.lower().startswith(b"host:")
        )
        print(json.dumps({"host": host, "path": path}), flush=True)
        body = b"<html><body>fixture document</body></html>"
        extra = b""
        status = b"200 OK"
        if path == "/redirect":
            status = b"302 Found"
            extra = b"Location: http://metadata.test/blocked\\r\\n"
        elif host == "rebind.test":
            status = b"302 Found"
            extra = b"Location: http://rebind.test/after\\r\\n"
        elif path == "/large":
            body = b"x" * (2 * 1024 * 1024 + 1)
        elif path == "/bomb":
            body = gzip.compress(b"x" * (4 * 1024 * 1024 + 1))
            extra = b"Content-Encoding: gzip\\r\\n"
        elif path == "/denied":
            status = b"403 Forbidden"
        elif path in {"/hold", "/hold-expiry"}:
            writer.write(
                b"HTTP/1.1 200 OK\\r\\nContent-Type: text/html\\r\\n"
                b"Connection: close\\r\\n\\r\\n"
            )
            await writer.drain()
            await reader.read()
            print(json.dumps({"closed": path}), flush=True)
            return
        writer.write(
            b"HTTP/1.1 " + status + b"\\r\\nContent-Type: text/html\\r\\n"
            b"Connection: close\\r\\n" + extra + b"\\r\\n"
        )
        for offset in range(0, len(body), 16384):
            writer.write(body[offset:offset+16384])
            await writer.drain()
    except (OSError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

async def main():
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain("/fixture/server.crt", "/fixture/server.key")
    tls.set_servername_callback(
        lambda _socket, name, _context: print(json.dumps({"sni":name}), flush=True)
    )
    http = await asyncio.start_server(handle, "0.0.0.0", 80)
    https = await asyncio.start_server(handle, "0.0.0.0", 443, ssl=tls)
    async with http, https:
        print("ready", flush=True)
        await asyncio.gather(http.serve_forever(), https.serve_forever())

asyncio.run(main())
"""
_DIRECT_NETWORK_PROBE = """import errno,json,os,socket
assert os.getuid()==10001
status=open("/proc/self/status").read()
assert "CapEff:\\t0000000000000000" in status
for key in os.environ:
    assert not any(word in key.upper() for word in ("API_KEY", "SUPABASE", "DATABASE", "SECRET"))
results=[]
for kind,port in ((socket.SOCK_STREAM,443),(socket.SOCK_DGRAM,53)):
    s=socket.socket(socket.AF_INET,kind)
    s.settimeout(1)
    try:
        if kind==socket.SOCK_STREAM:
            s.connect(("1.1.1.1",port))
        else:
            s.sendto(b"probe",("1.1.1.1",port))
    except OSError as error:
        assert error.errno in (errno.ENETUNREACH,errno.EHOSTUNREACH),error
        results.append(error.errno)
    else:
        raise AssertionError("direct network bypass succeeded")
    finally:
        s.close()
assert len(results)==2
print(json.dumps({"direct_tcp":"denied","direct_udp_dns":"denied","uid":os.getuid()}))
"""
_TLS_PROBE = """import asyncio,json,ssl,sys
async def main():
    token=json.load(sys.stdin)["token"]
    reader,writer=await asyncio.open_unix_connection("/run/trellis-egress/gateway.sock")
    writer.write(json.dumps({"lease_token":token,"host":"public.test","port":443}).encode()+b"\\n")
    await writer.drain()
    assert json.loads(await reader.readline())=={"status":"ok"}
    context=ssl.create_default_context()
    try:
        await writer.start_tls(context,server_hostname="public.test")
        print("pinned_peer_tls_hostname_pass")
    except ssl.SSLCertVerificationError as error:
        print(json.dumps({"verify_code":error.verify_code,"verify_message":error.verify_message}))
        raise
    finally:
        writer.close()
asyncio.run(main())
"""


_LEASE_EXPIRY_PROBE = """import json,socket,sys
token=json.load(sys.stdin)["token"]
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
    connection.settimeout(4)
    connection.connect("/run/trellis-egress/gateway.sock")
    connection.sendall(json.dumps({"lease_token":token,"host":"public.test","port":80}).encode()+b"\\n")
    stream=connection.makefile("rb")
    assert json.loads(stream.readline())=={"status":"ok"}
    connection.sendall(b"GET /hold-expiry HTTP/1.1\\r\\nHost: public.test\\r\\n\\r\\n")
    assert stream.readline().startswith(b"HTTP/1.1 200")
    while stream.readline()!=b"\\r\\n":
        pass
    assert stream.read(1)==b""
print("lease_expiry_closed_active_socket")
"""


async def _docker(*args: str, deadline_seconds: float = 15, limit: int = 65536) -> str:
    code, output = await _run_docker(
        args, deadline_seconds=deadline_seconds, limit=limit, capture_stderr=args[0] == "logs"
    )
    if code:
        raise RuntimeError(
            f"Docker {args[0]} failed with exit status {code}: {output[-8192:].decode()}"
        )
    return output.decode().strip()


async def _context_active() -> None:
    return None


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        "fixture",
        "fixture",
        "fixture",
        "fixture",
        "fixture",
        1,
        1,
        datetime.now(UTC) + timedelta(seconds=30),
        RunLimits(8, 16, 100000, 600, 2.0),
        None,
        _context_active,
    )


async def _openssl(directory: Path, *args: str) -> None:
    process = await asyncio.create_subprocess_exec(
        "openssl",
        *args,
        cwd=directory,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(15):
            code = await process.wait()
        if code:
            raise RuntimeError("fixture certificate generation failed")
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


def _gateway_args(
    name: str, network: str, volume: str, secret_file: Path, image: str
) -> tuple[str, ...]:
    return (
        "run",
        "--detach",
        "--name",
        name,
        "--network",
        network,
        "--ipc",
        "private",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "NET_ADMIN",
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "SETUID",
        "--cap-add",
        "SETGID",
        "--cap-add",
        "SETPCAP",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "256m",
        "--memory-swap",
        "256m",
        "--cpus",
        "1",
        "--pids-limit",
        "64",
        "--shm-size",
        "16m",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
        "--publish",
        "127.0.0.1::8081",
        "--mount",
        f"type=volume,src={volume},dst=/run/trellis-egress",
        "--mount",
        f"type=bind,src={secret_file},dst=/run/secrets/gateway-admin,readonly",
        image,
    )


async def verify() -> None:
    for source in (_GATEWAY, _FIXTURE, _TLS_PROBE, _DIRECT_NETWORK_PROBE, _LEASE_EXPIRY_PROBE):
        compile(source, "<trusted Docker proof fixture>", "exec")
    suffix = uuid4().hex[:12]
    gateway_name, fixture_name = "trellis-gateway-proof-" + suffix, "trellis-http-proof-" + suffix
    network, volume = "trellis-proof-net-" + suffix, "trellis-proof-ipc-" + suffix
    admin_network = "trellis-proof-admin-" + suffix
    probe_name = "trellis-direct-proof-" + suffix
    expiry_name = "trellis-expiry-proof-" + suffix
    production_name = "trellis-production-proof-" + suffix
    worker_tag, gateway_tag = (
        "trellis-proof-worker-base:" + suffix,
        "trellis-proof-gateway-base:" + suffix,
    )
    derived_images: list[str] = []
    with tempfile.TemporaryDirectory(prefix="trellis-isolation-") as temporary:
        directory = Path(temporary)
        secret = secrets.token_urlsafe(48)
        (directory / "admin").write_text(secret)
        (directory / "admin").chmod(0o444)
        (directory / "fixture.py").write_text(_FIXTURE)
        (directory / "gateway.py").write_text(_GATEWAY)
        try:
            worker_image = await _docker(
                "build",
                "--quiet",
                "-f",
                str(_BACKEND / "containers/worker.Dockerfile"),
                str(_BACKEND),
                deadline_seconds=240,
            )
            gateway_image = await _docker(
                "build",
                "--quiet",
                "-f",
                str(_BACKEND / "containers/gateway.Dockerfile"),
                str(_BACKEND),
                deadline_seconds=240,
            )
            print(
                json.dumps(
                    {
                        "production_worker_image": worker_image,
                        "production_gateway_image": gateway_image,
                    }
                ),
                flush=True,
            )
            await _docker("tag", worker_image, worker_tag)
            await _docker("tag", gateway_image, gateway_tag)
            await _openssl(
                directory,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=Trellis fixture CA",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
                "-addext",
                "subjectKeyIdentifier=hash",
                "-addext",
                "authorityKeyIdentifier=keyid:always",
                "-keyout",
                "ca.key",
                "-out",
                "ca.crt",
            )
            await _openssl(
                directory,
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-subj",
                "/CN=public.test",
                "-keyout",
                "server.key",
                "-out",
                "server.csr",
            )
            (directory / "extensions").write_text(
                "subjectAltName=DNS:public.test\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nauthorityKeyIdentifier=keyid:always,issuer:always\nsubjectKeyIdentifier=hash\n"
            )
            await _openssl(
                directory,
                "x509",
                "-req",
                "-days",
                "1",
                "-in",
                "server.csr",
                "-CA",
                "ca.crt",
                "-CAkey",
                "ca.key",
                "-CAcreateserial",
                "-extfile",
                "extensions",
                "-out",
                "server.crt",
            )
            (directory / "server.key").chmod(0o444)
            (directory / "worker.Dockerfile").write_text(
                f"FROM {worker_tag}\nUSER 0\n"
                "COPY ca.crt /usr/local/share/ca-certificates/trellis-fixture.crt\n"
                "RUN update-ca-certificates\nUSER 10001:10001\n"
            )
            fixture_worker = await _docker(
                "build",
                "--quiet",
                "-f",
                str(directory / "worker.Dockerfile"),
                str(directory),
                deadline_seconds=120,
            )
            derived_images.append(fixture_worker)
            (directory / "gateway.Dockerfile").write_text(
                f"FROM {gateway_tag}\n"
                "RUN mv app/infrastructure/web_gateway.py "
                "app/infrastructure/_production_gateway.py\n"
                "COPY gateway.py app/infrastructure/web_gateway.py\n"
                "COPY fixture.py /fixture/fixture.py\nCOPY server.crt server.key /fixture/\n"
            )
            fixture_gateway = await _docker(
                "build",
                "--quiet",
                "-f",
                str(directory / "gateway.Dockerfile"),
                str(directory),
                deadline_seconds=120,
            )
            derived_images.append(fixture_gateway)
            await _docker("network", "create", "--internal", "--subnet", "93.184.216.0/24", network)
            await _docker("network", "create", admin_network)
            await _docker("volume", "create", volume)
            await _docker(
                *_gateway_args(
                    production_name, admin_network, volume, directory / "admin", gateway_image
                )
            )
            production_observed = json.loads(await _docker("inspect", production_name))[0]
            production_port = production_observed["NetworkSettings"]["Ports"]["8081/tcp"][0][
                "HostPort"
            ]
            production_control = GatewayControlClient("http://127.0.0.1:" + production_port, secret)
            production = DockerPublicRetriever(
                WorkerDeployment(
                    worker_image, gateway_image, production_name, volume, directory / "admin"
                ),
                production_control,
            )
            for _ in range(30):
                if await production.is_ready():
                    break
                if "Traceback" in await _docker("logs", production_name):
                    raise AssertionError(await _docker("logs", production_name))
                await asyncio.sleep(0.1)
            else:
                code, detail = await _run_docker(
                    (
                        "exec",
                        "--user",
                        "0",
                        production_name,
                        "/usr/local/bin/trellis-check-egress-policy",
                    )
                )
                print(
                    json.dumps(
                        {
                            "production_guard": code,
                            "detail": detail.decode(),
                            "mounts": production_observed["Mounts"],
                            "host": production_observed["HostConfig"],
                            "control_ready": await production_control.is_ready(),
                        }
                    ),
                    flush=True,
                )
                raise AssertionError("production gateway readiness failed")
            dns_probe = (
                "import asyncio; "
                "from app.infrastructure.web_network import SystemResolver,public_address; "
                "answers=asyncio.run(SystemResolver().resolve('example.com',443)); "
                "assert answers and all(public_address(a) for a in answers); "
                "print('production_dns_pass')"
            )
            print(
                await _docker(
                    "exec", "--user", "10001:10001", production_name, "python", "-c", dns_probe
                ),
                flush=True,
            )
            await _docker("rm", "--force", production_name)
            await _docker(
                "run",
                "--detach",
                "--name",
                fixture_name,
                "--network",
                network,
                "--ip",
                _FIXTURE_IP,
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "10001:10001",
                "--memory",
                "128m",
                "--pids-limit",
                "32",
                fixture_gateway,
                "python",
                "/fixture/fixture.py",
            )
            await _docker(
                *_gateway_args(
                    gateway_name, admin_network, volume, directory / "admin", fixture_gateway
                )
            )
            await _docker("network", "connect", network, gateway_name)
            observed = json.loads(await _docker("inspect", gateway_name))[0]
            if not observed["State"]["Running"]:
                raise AssertionError(await _docker("logs", gateway_name))
            port = observed["NetworkSettings"]["Ports"]["8081/tcp"][0]["HostPort"]
            control = GatewayControlClient("http://127.0.0.1:" + port, secret)
            deployment = WorkerDeployment(
                fixture_worker, fixture_gateway, gateway_name, volume, directory / "admin"
            )
            retriever = DockerPublicRetriever(deployment, control)
            for _ in range(100):
                if await retriever.is_ready() and "ready" in await _docker("logs", fixture_name):
                    break
                await asyncio.sleep(0.1)
            else:
                code, detail = await _run_docker(
                    (
                        "exec",
                        "--user",
                        "0",
                        gateway_name,
                        "/usr/local/bin/trellis-check-egress-policy",
                    )
                )
                print(
                    json.dumps(
                        {
                            "guard_code": code,
                            "guard_detail": detail.decode(),
                            "caps": observed["HostConfig"]["CapAdd"],
                            "ports": observed["NetworkSettings"]["Ports"],
                            "logs": await _docker("logs", gateway_name),
                        }
                    ),
                    flush=True,
                )
                raise AssertionError("real gateway did not become ready")
            probe_args = _worker_args(deployment, probe_name)
            await _docker(*probe_args[:-3], "python", "-c", _DIRECT_NETWORK_PROBE)
            print(await _docker("start", "--attach", probe_name), flush=True)
            await _docker("rm", "--force", probe_name)
            await _docker(*probe_args[:-3], "python", "-c", _TLS_PROBE)
            tls_lease = await control.issue(RetrievalRequest("https://public.test/ok"), 10)
            code, diagnostic = await _run_docker(
                ("start", "--attach", "--interactive", probe_name),
                payload=json.dumps({"token": tls_lease}).encode(),
                deadline_seconds=10,
                capture_stderr=True,
            )
            await control.revoke(tls_lease)
            assert code == 0, diagnostic.decode()
            print(diagnostic.decode().strip(), flush=True)
            await _docker("rm", "--force", probe_name)
            result = await retriever.retrieve(
                RetrievalRequest("https://public.test/ok"), _context()
            )
            assert result.status is RetrievalStatus.SUCCESS, result
            assert result.body == b"<html><body>fixture document</body></html>"
            logs = await _docker("logs", fixture_name)
            assert '"sni": "public.test"' in logs and '"host": "public.test"' in logs
            before = len(logs.splitlines())
            for url in (
                "http://mixed.test/",
                "http://private.test/",
                "http://metadata.test/",
                "http://mapped.test/",
                "http://ipv6.test/",
                "http://127.0.0.1/",
                "http://169.254.169.254/",
                "http://[::1]/",
                "http://[::ffff:93.184.216.34]/",
            ):
                blocked = await retriever.retrieve(RetrievalRequest(url), _context())
                assert blocked.status is RetrievalStatus.BLOCKED, (url, blocked)
            assert len((await _docker("logs", fixture_name)).splitlines()) == before
            for path, expected in (
                ("/large", RetrievalStatus.OVERSIZED),
                ("/bomb", RetrievalStatus.OVERSIZED),
                ("/denied", RetrievalStatus.ACCESS_DENIED),
                ("/redirect", RetrievalStatus.BLOCKED),
            ):
                result = await retriever.retrieve(
                    RetrievalRequest("http://public.test" + path), _context()
                )
                assert result.status is expected, (path, result)
            rebind = await retriever.retrieve(RetrievalRequest("http://rebind.test/"), _context())
            assert rebind.status is RetrievalStatus.BLOCKED, rebind
            expiry_args = _worker_args(deployment, expiry_name)
            await _docker(*expiry_args[:-3], "python", "-c", _LEASE_EXPIRY_PROBE)
            lease_request = RetrievalRequest(
                "http://public.test/hold-expiry",
                max_requests=1,
                max_response_bytes=4096,
                timeout_seconds=2,
            )
            lease = await control.issue(lease_request, 2)
            code, output = await _run_docker(
                ("start", "--attach", "--interactive", expiry_name),
                payload=json.dumps({"token": lease}).encode(),
                deadline_seconds=5,
            )
            assert code == 0, output
            print(output.decode().strip(), flush=True)
            receipt = await control.revoke(lease)
            assert receipt is not None and receipt.tunnels == 1
            await _docker("rm", "--force", expiry_name)
            task = asyncio.create_task(
                retriever.retrieve(RetrievalRequest("http://public.test/hold"), _context())
            )
            for _ in range(100):
                if '"path": "/hold"' in await _docker("logs", fixture_name):
                    break
                await asyncio.sleep(0.1)
            else:
                raise AssertionError("cancellation fixture did not begin")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("retrieval cancellation did not propagate")
            for _ in range(20):
                if '"closed": "/hold"' in await _docker("logs", fixture_name):
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("cancellation left the remote socket open")
            assert not await _docker(
                "ps", "--all", "--quiet", "--filter", "label=trellis.role=retrieval-worker"
            )
        finally:
            for name in (probe_name, expiry_name, fixture_name, gateway_name, production_name):
                await _run_docker(("rm", "--force", name), deadline_seconds=10)
            await _run_docker(("network", "rm", network), deadline_seconds=10)
            await _run_docker(("network", "rm", admin_network), deadline_seconds=10)
            await _run_docker(("volume", "rm", volume), deadline_seconds=10)
            for image in derived_images:
                await _run_docker(("image", "rm", image), deadline_seconds=15)

            for resource, names in (
                (
                    "container",
                    (probe_name, expiry_name, fixture_name, gateway_name, production_name),
                ),
                ("network", (network, admin_network)),
                ("volume", (volume,)),
            ):
                for name in names:
                    code, output = await _run_docker((resource, "inspect", name))
                    absent = b"no such" in output.lower() or b"not found" in output.lower()
                    if code == 0 or not absent:
                        raise AssertionError(f"temporary {resource} cleanup is unconfirmed")

    print(
        json.dumps(
            {
                "result": "PASS",
                "tls_host_sni": "verified",
                "blocked_destinations": "zero connections",
                "overflow": "bounded",
                "cancellation": "worker removed, lease revoked",
                "kernel_policy": "inspected",
                "temporary_resources": "removed",
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    os.chdir(_BACKEND)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(verify())
