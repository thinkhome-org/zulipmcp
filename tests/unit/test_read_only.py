import httpx
import pytest

from thinkhome_zulip_mcp.executor import Executor, Principal

CALLER = Principal("alice", "https://zulip.example", "alice", "synthetic")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        "send-message",
        "update-status",
        "mute-user",
        "unmute-user",
        "update-message",
        "register-queue",
        "get-events",
        "get-stream-email-address",
        "create-big-blue-button-video-call",
        "summarize-messages",
    ],
)
async def test_read_only_denies_mutations_before_network(operation):
    def upstream(request):
        pytest.fail("Read-only policy allowed upstream side effect")

    executor = Executor(read_only=True, transport=httpx.MockTransport(upstream))
    result = await executor.execute(CALLER, operation, {})
    assert result.error["code"] == "read_only_policy"


@pytest.mark.asyncio
async def test_read_only_permits_history():
    def upstream(request):
        return httpx.Response(200, json={"result": "success", "messages": []})

    executor = Executor(read_only=True, transport=httpx.MockTransport(upstream))
    result = await executor.execute(
        CALLER, "get-messages", {"anchor": "newest", "num_before": 10, "num_after": 0}
    )
    assert result.error is None


def test_read_only_environment_is_strict(monkeypatch):
    monkeypatch.setenv("ZULIPCHAT_READ_ONLY", "true")
    assert Executor().read_only is True
    monkeypatch.setenv("ZULIPCHAT_READ_ONLY", "typo")
    with pytest.raises(ValueError):
        Executor()


@pytest.mark.asyncio
async def test_read_only_discovery_and_batch_cannot_bypass_policy():
    from thinkhome_zulip_mcp.server import create_server

    def upstream(request):
        pytest.fail("Write escaped read-only mode")

    server = create_server(
        CALLER, Executor(read_only=True, transport=httpx.MockTransport(upstream))
    )
    names = {tool.name for tool in await server.list_tools()}
    assert names == {
        "zulip_search_operations",
        "zulip_describe_operation",
        "zulip_execute",
        "zulip_batch",
        "zulip_history",
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
        "zulip_capabilities",
    }
    result = await server.call_tool(
        "zulip_batch",
        {"operations": [{"operation_id": "send-message", "arguments": {}}]},
    )
    assert result.is_error
    assert (
        result.structured_content["data"]["results"][0]["result"]["error"]["code"]
        == "read_only_policy"
    )
    caps = await server.call_tool("zulip_capabilities", {})
    assert caps.structured_content["read_only"] is True
