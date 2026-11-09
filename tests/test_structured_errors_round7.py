"""Round 7 of structured tool errors (task 0007): ON-mode fan-out _meta under fail-fast gather."""

import asyncio
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolRequest, CallToolRequestParams
from telethon import errors

from telegram_mcp import runtime


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    monkeypatch.setenv("TELEGRAM_TOOL_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setattr(runtime, "clients", {"a": object(), "b": object()})

    @runtime.mcp.tool(name="zz_round7_probe")
    @runtime.with_account(readonly=True)
    async def tool(spec: str, account: str = None) -> str:
        delay, action = dict(i.split("=", 1) for i in spec.split(","))[account].split(":")
        await asyncio.sleep(float(delay))
        if action == "flood":
            raise errors.FloodWaitError(request=None, capture=42)
        if action == "raise":
            raise RuntimeError("boom")
        if action == "tool":
            raise ToolError("plain failure")
        return "ok"

    yield
    runtime.mcp._tool_manager._tools.pop("zz_round7_probe", None)


async def _call(spec):
    handler = runtime.mcp._mcp_server.request_handlers[CallToolRequest]
    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name="zz_round7_probe", arguments={"spec": spec}),
    )
    return json.loads((await handler(req)).root.model_dump_json(by_alias=True))


def _entries(result):
    meta = (result.get("_meta") or {}).get("telegram-mcp/error") or {}
    return meta


@pytest.mark.asyncio
async def test_on_fast_flood_account_a_slow_ok_b_lists_only_a(probe):
    result = await _call("a=0.02:flood,b=0.2:ok")
    assert result["isError"] is True
    meta = _entries(result)
    text = json.dumps(meta)
    assert "flood" in text and "42" in text
    assert '"b"' not in text


@pytest.mark.asyncio
async def test_on_fast_raise_b_does_not_leak_flood_retry_after(probe):
    result = await _call("a=0.2:flood,b=0.02:raise")
    assert result["isError"] is True
    meta = _entries(result)
    text = json.dumps(meta)
    assert "42" not in text and "retry_after" not in text
    assert '"a"' not in text


@pytest.mark.asyncio
async def test_on_all_handled_failures_every_account_listed_with_own_kind(probe):
    result = await _call("a=0.02:tool,b=0.05:tool")
    assert result["isError"] is True
    accounts = _entries(result)["accounts"]
    assert [(e["account"], e["kind"]) for e in accounts] == [("a", "internal"), ("b", "internal")]
    assert all("retry_after" not in e for e in accounts)


@pytest.mark.asyncio
async def test_on_partial_failure_is_not_error_but_lists_failing_account_only(probe):
    result = await _call("a=0.02:ok,b=0.02:tool")
    assert not result.get("isError")
    accounts = _entries(result)["accounts"]
    assert [(e["account"], e["kind"]) for e in accounts] == [("b", "internal")]
