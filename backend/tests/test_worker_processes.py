import asyncio
import json
import os
import socket
import sys
from contextlib import suppress
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import psycopg
import pytest
from websockets.asyncio.client import connect
from websockets.typing import Origin

from tests.durable_harness import expire, seed

pytestmark = pytest.mark.skipif(
    not os.environ.get("TRELLIS_TEST_DATABASE_URL"),
    reason="Disposable migrated PostgreSQL URLs are required for process verification",
)


async def rpc(socket, method, params, events=None):
    request_id = str(uuid4())
    await socket.send(
        json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    )
    async with asyncio.timeout(8):
        while True:
            result = json.loads(await socket.recv())
            if result.get("id") == request_id or (result.get("id") is None and "error" in result):
                return result
            if events is not None and result.get("method") == "run.event":
                events.append(result["params"])


async def launch(*arguments, delay=".2", chunk_delay="0"):
    environment = {
        **os.environ,
        "TRELLIS_TEST_PROVIDER_DELAY": delay,
        "TRELLIS_TEST_PROVIDER_CHUNK_DELAY": chunk_delay,
    }
    return await asyncio.create_subprocess_exec(
        sys.executable,
        *arguments,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


async def stop(process):
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            async with asyncio.timeout(4):
                await process.wait()
        except TimeoutError:
            process.kill()
            await process.wait()
    output, _ = await process.communicate()
    return output.decode()


async def query(statement, parameters=()):
    async with await psycopg.AsyncConnection.connect(os.environ["TRELLIS_TEST_DATABASE_URL"]) as db:
        cursor = await db.execute(statement, parameters)
        return await cursor.fetchall() if cursor.description else []


async def await_status(run_id, expected):
    async with asyncio.timeout(12):
        while True:
            row = await query("SELECT status FROM trellis.runs WHERE id=%s", (run_id,))
            if row[0][0] == expected:
                return
            if row[0][0] in {"failed", "interrupted", "cancelled"} and expected == "completed":
                detail = await query(
                    "SELECT error_code,error_message FROM trellis.runs WHERE id=%s", (run_id,)
                )
                raise AssertionError(detail)
            await asyncio.sleep(0.05)


def test_independent_processes_preserve_accepted_work_and_fence_crashed_attempts():
    async def scenario():
        await seed()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        websocket_url = f"ws://127.0.0.1:{port}/api/runtime"
        children = []

        async def start_api():
            process = await launch(
                "-m",
                "uvicorn",
                "tests.durable_harness:create_api",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            )
            children.append(process)
            async with httpx.AsyncClient(trust_env=False) as client, asyncio.timeout(8):
                while True:
                    if process.returncode is not None:
                        raise AssertionError(await stop(process))
                    try:
                        if (await client.get(f"{base}/health")).status_code == 200:
                            return process
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(0.05)

        async def accept(token="alice"):
            async with httpx.AsyncClient(trust_env=False) as client:
                session = (
                    await client.post(
                        f"{base}/api/sessions", headers={"Authorization": f"Bearer {token}"}
                    )
                ).json()
            async with connect(websocket_url, origin=Origin("http://localhost:3000")) as ws:
                assert "result" in await rpc(ws, "auth.authenticate", {"accessToken": token})
                request_id = str(uuid4())
                params = {
                    "sessionId": session["id"],
                    "clientRequestId": request_id,
                    "content": "Continue when the browser closes.",
                }
                accepted = await rpc(ws, "run.start", params)
                assert "result" in accepted, accepted
                retry = await rpc(ws, "run.start", params)
                assert retry["result"]["runId"] == accepted["result"]["runId"]
                assert accepted["result"]["limits"]["deadlineAt"] is None
                return accepted["result"]["runId"], session["id"], request_id

        try:
            api = await start_api()
            run_id, session_id, request_id = await accept()
            assert (
                await query("SELECT status FROM trellis_private.jobs WHERE run_id=%s", (run_id,))
            )[0][0] == "ready"
            await stop(api)
            api = await start_api()
            async with connect(websocket_url, origin=Origin("http://localhost:3000")) as ws:
                await rpc(ws, "auth.authenticate", {"accessToken": "alice"})
                restored = await rpc(
                    ws, "run.lookup", {"sessionId": session_id, "clientRequestId": request_id}
                )
                assert restored["result"]["runId"] == run_id
            workers = []
            for _ in range(2):
                worker = await launch("-m", "tests.durable_harness", "worker")
                workers.append(worker)
                children.append(worker)
            await await_status(run_id, "completed")
            assert (
                await query(
                    "SELECT count(*) FROM trellis_private.execution_attempts WHERE run_id=%s",
                    (run_id,),
                )
            )[0][0] == 1
            async with connect(websocket_url, origin=Origin("http://localhost:3000")) as ws:
                await rpc(ws, "auth.authenticate", {"accessToken": "alice"})
                replay = []
                restored = await rpc(
                    ws, "run.resume", {"runId": run_id, "afterSequence": 0}, replay
                )
                async with asyncio.timeout(8):
                    while not any(event["eventType"] == "run.completed" for event in replay):
                        frame = json.loads(await ws.recv())
                        if frame.get("method") == "run.event":
                            replay.append(frame["params"])
                assert [event["sequence"] for event in replay] == list(
                    range(1, restored["result"]["lastSequence"] + 1)
                )
            async with connect(websocket_url, origin=Origin("http://localhost:3000")) as ws:
                await rpc(ws, "auth.authenticate", {"accessToken": "bob"})
                for method in ("run.resume", "run.cancel"):
                    denied = await rpc(ws, method, {"runId": run_id, "afterSequence": 0})
                    assert "error" in denied
            for worker in workers:
                await stop(worker)

            await stop(api)
            api = await start_api()
            short_token = f"alice:{datetime.now(UTC).timestamp() + 7}"
            expiry_run, _, _ = await accept(short_token)
            worker = await launch("-m", "tests.durable_harness", "worker", delay="8")
            children.append(worker)
            await await_status(expiry_run, "completed")
            assert datetime.now(UTC).timestamp() > float(short_token.partition(":")[2])
            await stop(worker)

            cancel_run, _, _ = await accept()
            worker = await launch("-m", "tests.durable_harness", "worker", delay="20")
            children.append(worker)
            await await_status(cancel_run, "running")
            async with connect(websocket_url, origin=Origin("http://localhost:3000")) as ws:
                await rpc(ws, "auth.authenticate", {"accessToken": "alice"})
                response = await rpc(ws, "run.cancel", {"runId": cancel_run})
                assert "result" in response
            await await_status(cancel_run, "cancelled")
            await stop(worker)

            for begun in (False, True):
                recovery_run, _, _ = await accept()
                if begun:
                    holder = await launch("-m", "tests.durable_harness", "worker", chunk_delay="20")
                    children.append(holder)
                    async with asyncio.timeout(8):
                        while True:
                            outputs = await query(
                                "SELECT content FROM trellis.run_assistant_outputs WHERE run_id=%s",
                                (recovery_run,),
                            )
                            if outputs and outputs[0][0] == "Durable worker ":
                                break
                            await asyncio.sleep(0.05)
                else:
                    holder = await launch("-m", "tests.durable_harness", "hold-claim")
                    children.append(holder)
                    async with asyncio.timeout(8):
                        claim = json.loads(await holder.stdout.readline())
                    assert claim["run_id"] == recovery_run
                holder.kill()
                await holder.wait()
                await expire(recovery_run)
                worker = await launch("-m", "tests.durable_harness", "worker")
                children.append(worker)
                await await_status(recovery_run, "interrupted" if begun else "completed")
                count = (
                    await query(
                        "SELECT count(*) FROM trellis.model_calls WHERE run_id=%s", (recovery_run,)
                    )
                )[0][0]
                assert count == 1
                attempts = (
                    await query(
                        "SELECT count(*) FROM trellis_private.execution_attempts WHERE run_id=%s",
                        (recovery_run,),
                    )
                )[0][0]
                assert attempts == (1 if begun else 2)
                if begun:
                    assert (
                        await query(
                            "SELECT content FROM trellis.run_assistant_outputs WHERE run_id=%s",
                            (recovery_run,),
                        )
                    )[0][0] == "Durable worker "
                await stop(worker)
            print(
                "API restart, duplicate acceptance, two workers, browser expiry, "
                "isolation, and crash fencing passed"
            )
        finally:
            for child in children:
                output = await stop(child)
                if child.returncode not in {0, -9, -15}:
                    print(output)

    asyncio.run(scenario())
