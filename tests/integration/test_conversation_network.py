"""Real loopback MCP transport with a simulated Zulip upstream."""

import asyncio
import socket
import time

import httpx
import httpx2
import pytest
import uvicorn
from cryptography.fernet import Fernet
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from thinkhome_zulip_mcp.auth import AuthConfig, Authenticator
from thinkhome_zulip_mcp.executor import Executor, Principal
from thinkhome_zulip_mcp.server import create_http_app
from thinkhome_zulip_mcp.store import IdentityStore


@pytest.mark.asyncio
@pytest.mark.parametrize("end_while_listening", [False, True])
async def test_network_listen_delivers_progress_message_and_cleans_queue(
    tmp_path, end_while_listening
):
    store = IdentityStore(tmp_path / "identities", Fernet.generate_key())
    store.add_identity(
        Principal("alice", "https://zulip.example", "alice", "synthetic")
    )
    token = store.issue_token("alice", expires_at=int(time.time()) + 120)
    waiting = asyncio.Event()
    polls = 0
    deleted = []

    async def upstream(request):
        nonlocal polls
        assert (
            request.headers["authorization"]
            == httpx.BasicAuth("alice", "synthetic")._auth_header
        )
        path = request.url.path
        data = {"result": "success"}
        if path.endswith("/users/me"):
            data |= {"user_id": 1, "max_message_id": 10}
        elif path.endswith("/streams/7"):
            data |= {"stream": {"stream_id": 7, "name": "work", "invite_only": False}}
        elif path.endswith("/messages"):
            data |= {"messages": [], "found_oldest": True, "found_newest": True}
        elif path.endswith("/users/1/subscriptions/7"):
            data |= {"is_subscribed": True}
        elif path.endswith("/realm/emoji"):
            data |= {"emoji": {}}
        elif path.endswith("/register"):
            data |= {"queue_id": "q", "last_event_id": -1}
        elif path.endswith("/events"):
            if request.method == "DELETE":
                deleted.append("q")
            else:
                await asyncio.sleep(0.05)
                polls += 1
                if polls == 2 and end_while_listening:
                    waiting.set()
                    await asyncio.Event().wait()
                event = (
                    {"id": 0, "type": "heartbeat"}
                    if polls == 1
                    else {
                        "id": 1,
                        "type": "message",
                        "message": {
                            "id": 11,
                            "type": "stream",
                            "stream_id": 7,
                            "subject": "topic",
                            "sender_id": 2,
                            "content": "network fixture message",
                            "reactions": [],
                        },
                    }
                )
                data |= {"events": [event]}
        else:
            assert path.endswith(("/typing", "/messages/10/reactions"))
        return httpx.Response(200, json=data)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        url = f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
        app = create_http_app(
            Authenticator(
                store,
                AuthConfig(url, "https://zulip.example", allow_insecure_http=True),
            ),
            Executor(transport=httpx.MockTransport(upstream)),
        )
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="error", access_log=False)
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(15):
                while not server.started:
                    if task.done():
                        await task
                        pytest.fail("Server stopped before startup")
                    await asyncio.sleep(0.01)
                async with httpx2.AsyncClient(
                    headers={"authorization": f"Bearer {token}"}, trust_env=False
                ) as client:
                    async with streamable_http_client(
                        url, http_client=client
                    ) as streams:
                        async with ClientSession(streams[0], streams[1]) as session:
                            await session.initialize()
                            result = await session.call_tool(
                                "zulip_set_context", {"stream": 7, "topic": "topic"}
                            )
                            assert not result.is_error
                            handle = result.structured_content["data"]["handle"]
                            progress = []

                            async def on_progress(value, total, message):
                                progress.append((value, total))

                            pending = asyncio.create_task(
                                session.call_tool(
                                    "zulip_listen",
                                    {"handle": handle, "timeout_hours": 0.001},
                                    progress_callback=on_progress,
                                )
                            )
                            if end_while_listening:
                                await asyncio.wait_for(waiting.wait(), 3)
                                ended = await session.call_tool(
                                    "zulip_end_session",
                                    {
                                        "handle": handle,
                                        "message": "",
                                    },
                                )
                                assert not ended.is_error
                                result = await asyncio.wait_for(pending, 3)
                                assert result.is_error
                                assert (
                                    result.structured_content["error"]["code"]
                                    == "conversation_cancelled"
                                )
                                assert deleted == ["q"]
                                capabilities = await session.call_tool(
                                    "zulip_capabilities", {}
                                )
                                assert not capabilities.is_error
                                return
                            result = await pending
                            assert not result.is_error
                            assert (
                                result.structured_content["data"]["messages"][0]["id"]
                                == 11
                            )
                            assert progress and progress[0][1] == 3.6
                            assert deleted == ["q"]
                            ended = await session.call_tool(
                                "zulip_end_session", {"handle": handle, "message": ""}
                            )
                            assert not ended.is_error
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)
