"""Source-backed channel schema corrections must keep strict validation."""

import httpx
import pytest

from thinkhome_zulip_mcp.catalog import load_document
from thinkhome_zulip_mcp.executor import Executor, Principal


def test_subscribe_color_overlay_retains_strict_item_schema():
    definition = load_document()["paths"]["/users/me/subscriptions"]["post"]
    item = definition["requestBody"]["content"]["application/x-www-form-urlencoded"][
        "schema"
    ]["properties"]["subscriptions"]["items"]
    assert item["properties"]["color"]["type"] == "string"
    assert item["additionalProperties"] is False
    assert set(item["properties"]) == {"name", "description", "color"}
    assert (
        definition["x-thinkhome-parameter-audit"]["source_commit"]
        == "b31982251ff82d0b02e59f63ad21f19204e37aea"
    )


@pytest.mark.asyncio
async def test_subscribe_color_does_not_accept_unknown_nested_fields():
    def upstream(request):
        pytest.fail("invalid nested field reached upstream")

    result = await Executor(transport=httpx.MockTransport(upstream)).execute(
        Principal("a", "https://zulip.example", "a", "synthetic"),
        "subscribe",
        {"subscriptions": [{"name": "eng", "color": "#abc", "unexpected": True}]},
    )
    assert result.error["code"] == "invalid_arguments"
