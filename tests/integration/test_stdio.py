"""A real child process must speak MCP on stdout with no Zulip network access."""

import sys

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@pytest.mark.asyncio
async def test_stdio_initialize_list_and_catalog_call(tmp_path):
    secret = tmp_path / "key"
    secret.write_text("synthetic-stdio-secret")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "thinkhome_zulip_mcp", "serve", "--transport", "stdio"],
        env={
            "ZULIP_URL": "https://unused.example.invalid",
            "ZULIP_EMAIL": "test@example.invalid",
            "ZULIP_API_KEY_FILE": str(secret),
        },
    )
    with (tmp_path / "stderr").open("w+") as errors:
        async with stdio_client(params, errlog=errors) as (read, write):
            async with ClientSession(read, write) as session:
                result = await session.initialize()
                assert result.server_info.name == "ThinkHome Zulip MCP"
                listed = await session.list_tools()
                assert {t.name for t in listed.tools} == {
                    "zulip_search_operations",
                    "zulip_describe_operation",
                    "zulip_execute",
                    "zulip_batch",
                    "zulip_history",
                    "zulip_set_context",
                    "zulip_end_session",
                    "zulip_reply",
                    "zulip_listen",
                    "zulip_edit_message",
                    "zulip_get_message",
                    "zulip_get_users",
                    "zulip_get_user",
                    "zulip_get_own_user",
                    "zulip_resolve_user",
                    "zulip_get_presence",
                    "zulip_get_user_status",
                    "zulip_get_user_groups",
                    "zulip_get_user_group_members",
                    "zulip_is_user_group_member",
                    "zulip_update_status",
                    "zulip_manage_user_mute",
                    "zulip_add_reaction",
                    "zulip_remove_reaction",
                    "zulip_send_message",
                    "zulip_send_direct_message",
                    "zulip_move_messages",
                    "zulip_resolve_topic",
                    "zulip_capabilities",
                    "zulip_download_file",
                    "zulip_register_events",
                    "zulip_poll_events",
                    "zulip_delete_events",
                }
                batch = await session.call_tool(
                    "zulip_batch",
                    {
                        "operations": [
                            {"operation_id": "unknown", "arguments": {}},
                            {"operation_id": "unknown", "arguments": {}},
                        ]
                    },
                )
                assert batch.is_error
                assert batch.structured_content["data"]["not_attempted"] == [1]
                search = await session.call_tool(
                    "zulip_search_operations", {"query": "send-message"}
                )
                assert (
                    search.structured_content["operations"][0]["operation_id"]
                    == "send-message"
                )
                caps = await session.call_tool("zulip_capabilities", {})
                assert (
                    caps.structured_content["permissions"]
                    == "unknown; enforced by Zulip"
                )
                assert caps.structured_content["full_coverage"] is False
                rejected = await session.call_tool(
                    "zulip_execute", {"operation_id": "unknown", "arguments": {}}
                )
                assert rejected.is_error
                assert (
                    rejected.structured_content["error"]["code"] == "unknown_operation"
                )
        errors.seek(0)
        assert "synthetic-stdio-secret" not in errors.read()


@pytest.mark.asyncio
async def test_stdio_execute_reaches_only_the_configured_test_server(tmp_path):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs

    received = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(
                (
                    self.path,
                    parse_qs(
                        self.rfile.read(int(self.headers["Content-Length"])).decode()
                    ),
                )
            )
            body = json.dumps({"result": "success", "id": 73}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    secret = tmp_path / "key"
    secret.write_text("synthetic-local-key")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "thinkhome_zulip_mcp", "serve", "--transport", "stdio"],
        env={
            "ZULIP_URL": f"http://127.0.0.1:{upstream.server_port}",
            "ZULIP_EMAIL": "test@example.invalid",
            "ZULIP_API_KEY_FILE": str(secret),
            "ZULIP_ALLOW_INSECURE_HTTP": "1",
        },
    )
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "zulip_execute",
                    {
                        "operation_id": "send-message",
                        "arguments": {
                            "type": "stream",
                            "to": 10,
                            "topic": "Testing",
                            "content": "hello",
                        },
                    },
                )
                assert not result.is_error
                assert result.structured_content["data"]["id"] == 73
        assert received == [
            (
                "/api/v1/messages",
                {
                    "type": ["stream"],
                    "to": ["10"],
                    "topic": ["Testing"],
                    "content": ["hello"],
                },
            )
        ]
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_stdio_stages_only_files_under_configured_root(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    (root / "hello.txt").write_bytes(b"hello")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "thinkhome_zulip_mcp", "serve", "--transport", "stdio"],
        env={
            "ZULIP_URL": "https://unused.example.invalid",
            "ZULIP_EMAIL": "test@example.invalid",
            "ZULIP_API_KEY": "synthetic",
            "ZULIP_FILE_ROOT": str(root),
            "TMPDIR": str(runtime),
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert "zulip_stage_file" in {tool.name for tool in tools.tools}
            result = await session.call_tool("zulip_stage_file", {"path": "hello.txt"})
            assert not result.is_error
            assert result.structured_content["size"] == 5
            assert result.structured_content["filename"] == "hello.txt"
            assert len(result.structured_content["handle"]) > 30
            denied = await session.call_tool("zulip_stage_file", {"path": "../outside"})
            assert denied.is_error
    assert list(runtime.glob("zulip-mcp-*")) == []
