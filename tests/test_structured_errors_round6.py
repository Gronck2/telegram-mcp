"""Round 6 of structured tool errors (task 0007): switch-OFF parity of fan-out and privacy.

The golden file ``data/structured_errors_off_golden_fanout.json`` is recorded on the base
commit 0316bc6 (``RECORD_STRUCTURED_GOLDEN=1`` in a detached worktree) and compared at HEAD:
CallToolResult text / isError / _meta for multi-account read-only calls where accounts fail
or hang at different times, cancellation, and the set_privacy_settings refusal text. The
file imports only what exists on the base commit.

There are no known OFF differences: fan-out stays fail-fast exactly as on the base.
"""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError  # noqa: F401
from mcp.types import CallToolRequest, CallToolRequestParams
from telethon import functions, types
from telethon.tl.types import User

from telegram_mcp import runtime
from telegram_mcp.tools import profile  # noqa: F401  (registers the tools)

GOLDEN = Path(__file__).parent / "data" / "structured_errors_off_golden_fanout.json"
RECORD = bool(os.environ.get("RECORD_STRUCTURED_GOLDEN"))
TIMEOUT = 0.5

CHILDREN: dict = {}


@pytest.fixture
def probe(monkeypatch):
    """Read-only fan-out tool.

    ``spec`` says what each account does: ``label=delay:action:text``.
    """
    monkeypatch.delenv("TELEGRAM_STRUCTURED_ERRORS", raising=False)
    monkeypatch.setenv("TELEGRAM_TOOL_TIMEOUT_SECONDS", str(TIMEOUT))
    monkeypatch.setattr(runtime, "clients", {"a": object(), "b": object()})
    CHILDREN.clear()

    @runtime.mcp.tool(name="zz_round6_probe")
    @runtime.with_account(readonly=True)
    async def tool(spec: str, account: str = None) -> str:
        plan = dict(item.split("=", 1) for item in spec.split(","))[account]
        delay, action, message = plan.split(":", 2)
        try:
            await asyncio.sleep(float(delay))
        except asyncio.CancelledError:
            CHILDREN[account] = "cancelled"
            raise
        if action == "raise":
            raise RuntimeError(message)
        if action == "value":
            raise ValueError(message)
        if action == "tool":
            raise ToolError(message)
        return message

    yield
    runtime.mcp._tool_manager._tools.pop("zz_round6_probe", None)


def _request(spec):
    return CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name="zz_round6_probe", arguments={"spec": spec}),
    )


async def _call(spec):
    handler = runtime.mcp._mcp_server.request_handlers[CallToolRequest]
    return (await handler(_request(spec))).root


def _dump(result):
    return json.loads(result.model_dump_json(by_alias=True))


# scenario -> spec (delays in seconds; TIMEOUT is 0.5)
SCENARIOS = {
    # (a) one account raises quickly, the other succeeds slowly (inside the timeout)
    "a_fast_raise_other_slow_ok": "a=0.02:raise:A boom,b=0.25:ok:B ok",
    "a_slow_ok_other_fast_raise": "a=0.25:ok:A ok,b=0.02:raise:B boom",
    "a_fast_raise_other_slow_ok_beyond_timeout": "a=0.02:raise:A boom,b=5:ok:B ok",
    # (b) both raise different non-ToolErrors in different orders / timings
    "b_label_a_first": "a=0.02:raise:A early,b=0.15:value:B late",
    "b_label_b_first": "a=0.15:raise:A late,b=0.02:value:B early",
    "b_same_time": "a=0.05:raise:A same,b=0.05:value:B same",
    "b_toolerror_and_raise": "a=0.15:tool:A tool,b=0.02:raise:B raw",
    # (c) one account hangs past the per-tool timeout
    "c_hang_other_ok": "a=5:ok:A hung,b=0.02:ok:B ok",
    "c_hang_other_raise": "a=5:ok:A hung,b=0.02:raise:B boom",
    "c_hang_other_toolerror": "a=5:ok:A hung,b=0.02:tool:B tool",
    "c_both_ok_fast": "a=0.02:ok:A ok,b=0.02:ok:B ok",
}
# upper bound on wall time at HEAD per scenario (timeout + slack)
BOUND = TIMEOUT + 0.4


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCENARIOS)
async def test_switch_off_fanout_result_matches_base(probe, name):
    started = time.monotonic()
    result = _dump(await _call(SCENARIOS[name]))
    elapsed = time.monotonic() - started
    entry = {"result": result, "elapsed": round(elapsed, 2)}
    if RECORD:
        recorded = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
        recorded[name] = entry
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(recorded, indent=1, sort_keys=True, ensure_ascii=False))
        return
    assert elapsed <= BOUND, f"{name} took {elapsed:.2f}s"
    assert result.get("_meta") is None
    expected = json.loads(GOLDEN.read_text())[name]
    assert result == expected["result"], name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name, spec, cancel_after",
    [
        ("d_cancel_both_pending", "a=5:ok:A,b=5:ok:B", 0.05),
        ("d_cancel_one_failed_one_pending", "a=0.02:raise:A boom,b=5:ok:B", 0.1),
        ("d_cancel_after_done", "a=0.02:ok:A ok,b=0.02:ok:B ok", 0.2),
    ],
)
async def test_switch_off_cancellation_matches_base(probe, name, spec, cancel_after):
    task = asyncio.ensure_future(_call(spec))
    started = time.monotonic()
    await asyncio.sleep(cancel_after)
    task.cancel()
    try:
        outcome = {"result": _dump(await task)}
    except asyncio.CancelledError:
        outcome = {"cancelled": True}
    await asyncio.sleep(0.05)
    outcome["children_cancelled"] = sorted(CHILDREN)
    elapsed = time.monotonic() - started
    entry = {"result": outcome, "elapsed": round(elapsed, 2)}
    if RECORD:
        recorded = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
        recorded[name] = entry
        GOLDEN.write_text(json.dumps(recorded, indent=1, sort_keys=True, ensure_ascii=False))
        return
    assert elapsed <= cancel_after + 0.4
    assert outcome == json.loads(GOLDEN.read_text())[name]["result"], name


# --- set_privacy_settings: allow list fails to resolve, disallow list has entries -------

IVAN = User(id=101, first_name="Ivan", username="ivan_k", access_hash=11)


class PrivacyClient:
    def __init__(self):
        self.sent = []

    async def __call__(self, request):
        if isinstance(request, functions.account.GetPrivacyRequest):
            return types.account.PrivacyRules(
                rules=[types.PrivacyValueAllowAll()], chats=[], users=[IVAN]
            )
        self.sent.append(request)
        return types.account.PrivacyRules(rules=[], chats=[], users=[])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case, allow, disallow",
    [
        ("allow_unresolved_disallow_ok", ["@nobody"], ["@ivan_k"]),
        ("allow_unresolved_disallow_unresolved", ["@nobody"], ["@ghost"]),
        ("allow_ok_disallow_unresolved", ["@ivan_k"], ["@nobody"]),
    ],
)
async def test_set_privacy_settings_off_text_matches_base(monkeypatch, case, allow, disallow):
    monkeypatch.delenv("TELEGRAM_STRUCTURED_ERRORS", raising=False)
    client = PrivacyClient()
    resolved = []

    async def fake_resolve(identifier, cl=None):
        resolved.append(identifier)
        if identifier == "@ivan_k":
            return IVAN
        raise ValueError("No user has that username")

    monkeypatch.setattr(profile, "get_client", lambda account=None: client)
    monkeypatch.setattr(profile, "resolve_entity", fake_resolve)
    text = await profile.set_privacy_settings(
        key="phone", allow_users=allow, disallow_users=disallow
    )
    assert client.sent == []  # nothing changed on a failed resolve
    key = f"privacy/{case}"
    if RECORD:
        recorded = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
        recorded[key] = {"result": text, "resolved": resolved}
        GOLDEN.write_text(json.dumps(recorded, indent=1, sort_keys=True, ensure_ascii=False))
        return
    expected = json.loads(GOLDEN.read_text())[key]
    assert text == expected["result"]
    assert resolved == expected["resolved"]


@pytest.mark.asyncio
async def test_set_privacy_settings_first_failure_stays_authoritative(monkeypatch):
    from telethon import errors

    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    recorder = runtime._ErrorRecorder()
    token = runtime._error_recorder.set(recorder)
    resolved = []

    async def fake_resolve(identifier, cl=None):
        resolved.append(identifier)
        if identifier == "@flooded":
            raise errors.FloodWaitError(request=None, capture=31)
        raise ValueError("No user has that username")

    monkeypatch.setattr(profile, "get_client", lambda account=None: PrivacyClient())
    monkeypatch.setattr(profile, "resolve_entity", fake_resolve)
    try:
        await profile.set_privacy_settings(
            key="phone", allow_users=["@flooded"], disallow_users=["@nobody"]
        )
    finally:
        runtime._error_recorder.reset(token)
    assert resolved == ["@flooded", "@nobody"]  # same lookups as the base
    meta, _ = runtime._error_meta(recorder, False)
    assert meta == {"kind": "flood", "retry_after": 31}
