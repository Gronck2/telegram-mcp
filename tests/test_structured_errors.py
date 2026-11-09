"""Opt-in structured tool errors (task 0007, ADR 0003).

Probe tools are registered on the real FastMCP instance and called through the real
low-level CallToolRequest handler, so the call hook, FastMCP wrapping and the wire
result are the production ones.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolRequest, CallToolRequestParams
from telethon import errors

from telegram_mcp import runtime
from telegram_mcp.runtime import (
    AliasNeedsUser,
    ChatAccessDeniedError,
    EntityNotFoundError,
    ErrorCategory,
    ValidationError,
    classify_error,
    log_and_format_error,
    record_tool_error,
    with_account,
)

META = "telegram-mcp/error"
PROBE = "zz_structured_probe"
PROBE_WRITE = "zz_structured_probe_write"


def _raising(mode):
    flood = errors.FloodWaitError(request=None, capture=30)
    table = {
        "flood": flood,
        "slowmode": errors.SlowModeWaitError(request=None, capture=7),
        "allowlist": ChatAccessDeniedError("denied"),
        "notfound": EntityNotFoundError("nope"),
        "rpc_notfound": errors.PeerIdInvalidError(request=None),
        "private": errors.ChannelPrivateError(request=None),
        "admin": errors.ChatAdminRequiredError(request=None),
        "other_rpc": errors.AboutTooLongError(request=None),
        "validation": ValidationError("bad"),
        "alias": AliasNeedsUser("Ask the user which contact is meant."),
        "unknown": RuntimeError("hostile secret text"),
    }
    return table[mode]


def _failure(mode):
    error = _raising(mode)
    prefix = None
    if isinstance(error, ChatAccessDeniedError):
        prefix = ErrorCategory.PRIVACY
    return log_and_format_error(PROBE, error, prefix=prefix)


@pytest.fixture
def probes():
    @runtime.mcp.tool(name=PROBE)
    @with_account(readonly=True)
    async def probe(mode: str, fail_for: str = "", account: str = None) -> str:
        if mode == "ok":
            return "fine"
        if mode == "lookalike":
            return "An error occurred (code: X): quoted from a message."
        if mode == "per_account":
            return _failure("flood") if account in fail_for.split(",") else "fine"
        if mode == "toolerror":
            raise ToolError(_failure("private"))
        if mode == "bare_toolerror":
            raise ToolError("raw")
        if mode == "sleep":
            await asyncio.sleep(5)
        return _failure(mode)

    @runtime.mcp.tool(name=PROBE_WRITE)
    @with_account(readonly=False)
    async def probe_write(account: str = None) -> str:
        return "written"

    yield
    for name in (PROBE, PROBE_WRITE):
        runtime.mcp._tool_manager._tools.pop(name, None)


@pytest.fixture
def single(monkeypatch, probes):
    monkeypatch.setattr(runtime, "clients", {"one": SimpleNamespace()})


@pytest.fixture
def multi(monkeypatch, probes):
    monkeypatch.setattr(runtime, "clients", {"a": SimpleNamespace(), "b": SimpleNamespace()})


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")


@pytest.fixture
def off(monkeypatch):
    monkeypatch.delenv("TELEGRAM_STRUCTURED_ERRORS", raising=False)


async def call(name=PROBE, **arguments):
    request = CallToolRequest(
        method="tools/call", params=CallToolRequestParams(name=name, arguments=arguments)
    )
    handler = runtime.mcp._mcp_server.request_handlers[CallToolRequest]
    return (await handler(request)).root


def text(result):
    return "\n".join(b.text for b in result.content if b.type == "text")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,expected",
    [
        ("flood", {"kind": "flood", "retry_after": 30}),
        ("slowmode", {"kind": "flood", "rpc_error": "SlowModeWaitError", "retry_after": 7}),
        ("allowlist", {"kind": "forbidden_allowlist"}),
        ("notfound", {"kind": "not_found"}),
        ("rpc_notfound", {"kind": "not_found", "rpc_error": "PEER_ID_INVALID"}),
        ("private", {"kind": "private", "rpc_error": "CHANNEL_PRIVATE"}),
        ("admin", {"kind": "private", "rpc_error": "CHAT_ADMIN_REQUIRED"}),
        ("other_rpc", {"kind": "invalid_argument", "rpc_error": "ABOUT_TOO_LONG"}),
        ("validation", {"kind": "invalid_argument"}),
        ("alias", {"kind": "invalid_argument", "needs_user": True}),
        ("unknown", {"kind": "internal"}),
    ],
)
async def test_kinds_with_text_unchanged(single, on, mode, expected):
    baseline = _failure(mode)
    result = await call(mode=mode, account="one")
    assert result.isError is True
    assert result.meta == {META: expected}
    assert text(result) == baseline
    assert "hostile" not in json.dumps(result.meta)


@pytest.mark.asyncio
async def test_off_is_byte_identical_for_errors_and_success(single, off):
    for mode in ("flood", "allowlist", "unknown", "ok", "lookalike"):
        result = await call(mode=mode, account="one")
        assert result.isError is False
        assert result.meta is None
        dumped = result.model_dump(by_alias=True, exclude_none=True)
        assert "_meta" not in dumped
        assert dumped["isError"] is False
    flood = await call(mode="flood", account="one")
    assert text(flood) == (
        "Rate limit exceeded (FloodWait): Telegram requires waiting 30 seconds before "
        f"repeating this operation. Do NOT retry immediately (code: {_code(flood)})."
    )
    timed = await _timeout(0.05)
    assert timed.isError is True and timed.meta is None


def _code(result):
    return text(result).rsplit("code: ", 1)[1].rstrip(").")


async def _timeout(seconds):
    import os

    os.environ["TELEGRAM_TOOL_TIMEOUT_SECONDS"] = str(seconds)
    try:
        return await call(mode="sleep", account="one")
    finally:
        del os.environ["TELEGRAM_TOOL_TIMEOUT_SECONDS"]


@pytest.mark.asyncio
async def test_success_is_identical_with_switch_on_or_off(single, monkeypatch):
    monkeypatch.delenv("TELEGRAM_STRUCTURED_ERRORS", raising=False)
    before = await call(mode="ok", account="one")
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    after = await call(mode="ok", account="one")
    assert before.model_dump(by_alias=True) == after.model_dump(by_alias=True)
    assert after.isError is False and after.meta is None


@pytest.mark.asyncio
async def test_error_lookalike_success_is_not_flagged(single, on):
    result = await call(mode="lookalike", account="one")
    assert result.isError is False
    assert result.meta is None
    assert text(result).startswith("An error occurred")


@pytest.mark.asyncio
async def test_tool_error_path_carries_kind(single, on):
    result = await call(mode="toolerror", account="one")
    assert result.isError is True
    assert result.meta == {META: {"kind": "private", "rpc_error": "CHANNEL_PRIVATE"}}


@pytest.mark.asyncio
async def test_unrecorded_toolerror_is_invalid_argument(single, on):
    result = await call(mode="bare_toolerror", account="one")
    assert result.isError is True
    assert result.meta == {META: {"kind": "invalid_argument"}}


@pytest.mark.asyncio
async def test_missing_account_is_invalid_argument(multi, on):
    result = await call(PROBE_WRITE)
    assert result.isError is True
    assert result.meta == {META: {"kind": "invalid_argument"}}
    assert text(result).startswith("Error: 'account' is required.")


@pytest.mark.asyncio
async def test_missing_account_off_is_plain_success(multi, off):
    result = await call(PROBE_WRITE)
    assert result.isError is False and result.meta is None


@pytest.mark.asyncio
async def test_timeout_kind(single, on):
    result = await _timeout(0.05)
    assert result.isError is True
    assert result.meta == {META: {"kind": "timeout"}}
    assert "GEN-TIMEOUT" in text(result)


@pytest.mark.asyncio
async def test_fanout_partial_failure_is_not_an_error(multi, on):
    result = await call(mode="per_account", fail_for="a")
    assert result.isError is False
    meta = result.meta[META]
    assert meta["kind"] == "flood"
    assert meta["accounts"] == [{"kind": "flood", "retry_after": 30, "account": "a"}]
    assert text(result).startswith("[a]\n")


@pytest.mark.asyncio
async def test_fanout_all_failed_is_an_error(multi, on):
    result = await call(mode="per_account", fail_for="a,b")
    assert result.isError is True
    meta = result.meta[META]
    assert meta["kind"] == "flood"
    assert [a["account"] for a in meta["accounts"]] == ["a", "b"]


@pytest.mark.asyncio
async def test_fanout_all_toolerror_and_mixed_kinds(multi, on):
    result = await call(mode="toolerror")
    assert result.isError is True
    assert result.meta[META]["kind"] == "private"
    assert len(result.meta[META]["accounts"]) == 2


@pytest.mark.asyncio
async def test_fanout_success_has_no_meta(multi, on):
    result = await call(mode="ok")
    assert result.isError is False and result.meta is None


@pytest.mark.asyncio
async def test_validate_id_range_error_and_raise_errors_tool(monkeypatch, on):
    from telegram_mcp.tools import groups  # noqa: F401  (registers get_participants)

    monkeypatch.setattr(runtime, "clients", {"one": SimpleNamespace()})
    result = await call("get_participants", chat_id=2**70, account="one")
    assert result.isError is True
    assert result.meta == {META: {"kind": "invalid_argument"}}
    assert "out of the valid integer range" in text(result)


def test_allowlist_refusal_classification(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    message = log_and_format_error(
        "x", ChatAccessDeniedError("m"), prefix=ErrorCategory.PRIVACY, user_message="m"
    )
    assert message == "m"
    assert classify_error(ChatAccessDeniedError("m"))["kind"] == "forbidden_allowlist"


def test_record_without_recorder_is_noop():
    record_tool_error("internal")  # no call in flight: must not raise


def test_classify_never_uses_text():
    assert classify_error(RuntimeError("Rate limit exceeded (FloodWait)"))["kind"] == "internal"
    assert classify_error(ValueError("not found"))["kind"] == "internal"


@pytest.mark.asyncio
async def test_leak_check_meta_has_only_fixed_fields(single, on):
    for mode in ("unknown", "allowlist", "alias", "rpc_notfound", "flood"):
        result = await call(mode=mode, account="one")
        meta = result.meta[META]
        assert set(meta) <= {"kind", "retry_after", "rpc_error", "needs_user", "accounts"}
        assert "hostile" not in json.dumps(result.meta)


def test_flood_subclasses_with_seconds_are_flood_with_retry_after():
    for cls in (errors.FloodPremiumWaitError, errors.FloodTestPhoneWaitError):
        info = classify_error(cls(request=None, capture=12))
        assert info["kind"] == "flood" and info["retry_after"] == 12


def test_rpc_fallback_by_base_class():
    from telethon.errors.rpcbaseerrors import ForbiddenError, ServerError

    assert classify_error(errors.AboutTooLongError(request=None))["kind"] == "invalid_argument"
    assert classify_error(ForbiddenError(request=None, message="X_Y"))["kind"] == "private"
    assert classify_error(ServerError(request=None, message="X_Y"))["kind"] == "internal"
    # curated sets still win over the base-class fallback
    assert classify_error(errors.PeerIdInvalidError(request=None))["kind"] == "not_found"


def test_overlong_rpc_error_name_is_dropped():
    from telethon.errors.rpcbaseerrors import ServerError

    info = classify_error(ServerError(request=None, message="A" * 200))
    assert info["kind"] == "internal"
    assert "A" * 65 not in json.dumps(info)
    assert len(info["rpc_error"]) <= 64


def test_get_client_unknown_account_is_invalid_reference(monkeypatch):
    monkeypatch.setattr(runtime, "clients", {"a": SimpleNamespace()})
    with pytest.raises(runtime.InvalidReferenceError):
        runtime.get_client("zzz")
    assert classify_error(runtime.InvalidReferenceError("x")) == {"kind": "invalid_argument"}


@pytest.mark.asyncio
async def test_fastmcp_input_error_and_unknown_tool_are_invalid_argument(single, on):
    for result in (await call(PROBE, nonsense=1), await call("no_such_tool_zz")):
        assert result.isError is True
        assert result.meta == {META: {"kind": "invalid_argument"}}


@pytest.mark.asyncio
async def test_no_labelless_accounts_entry(multi, on):
    result = await call(mode="bare_toolerror")
    assert result.isError is True
    for entry in result.meta[META].get("accounts", []):
        assert "account" in entry
