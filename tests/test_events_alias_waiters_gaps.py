"""Gap tests for task 0012: get_messages_after and the structured-errors flag.

The waiter tools must treat an alias id as exact (no -100N / -N lookups) while plain ints
keep the marked fallback; refusal kinds under TELEGRAM_STRUCTURED_ERRORS stay unchanged.
Stub clients only.
"""

from collections import deque

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams
from telethon import types

from telegram_mcp import runtime
from telegram_mcp.tools import events

ID = 7654321
CHANNEL = -1000000000000 - ID
GROUP = -ID
WORDING = "мой контакт"


class Stub:
    def __init__(self, known=()):
        self.known = set(known)
        self.lookups = []

    async def get_dialogs(self):
        return []

    async def get_entity(self, identifier):
        return self._look(identifier)

    async def get_input_entity(self, identifier):
        return self._look(identifier)

    async def get_messages(self, *args, **kwargs):
        return []

    def _look(self, identifier):
        self.lookups.append(identifier)
        if identifier in self.known:
            return types.Channel(
                id=ID,
                title="T",
                photo=types.ChatPhotoEmpty(),
                date=None,
                access_hash=1,
                megagroup=True,
            )
        raise ValueError(f"Could not find the input entity for {identifier!r}")

    def marked(self):
        return [m for m in (CHANNEL, GROUP) if m in self.lookups]


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for name in (
        "TELEGRAM_ALLOWED_CHAT_IDS",
        "TELEGRAM_ALLOWED_READ_CHATS",
        "TELEGRAM_ALLOWED_WRITE_CHATS",
        "TELEGRAM_STRUCTURED_ERRORS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", None)
    monkeypatch.setenv("TELEGRAM_ALIASES_FILE", str(tmp_path / "aliases.json"))
    runtime.save_aliases({WORDING: ID})
    monkeypatch.setattr(events, "_event_log", deque(maxlen=2000))
    monkeypatch.setattr(events, "_event_seq", 0)
    monkeypatch.setattr(events, "_event_signal", None)
    monkeypatch.setattr(events, "_pending_msgs", {})
    monkeypatch.setattr(events, "_activity_event", None)

    async def connected(client=None):
        return None

    monkeypatch.setattr(runtime, "ensure_connected", connected)
    monkeypatch.setattr(events, "ensure_connected", connected, raising=False)


def setup(monkeypatch, allowed, known):
    if allowed is not None:
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", allowed)
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", None)
    stub = Stub(known)
    monkeypatch.setattr(runtime, "clients", {"one": stub})
    return stub


async def call(name, **arguments):
    request = CallToolRequest(
        method="tools/call", params=CallToolRequestParams(name=name, arguments=arguments)
    )
    result = (await runtime.mcp._mcp_server.request_handlers[CallToolRequest](request)).root
    body = "\n".join(b.text for b in result.content if b.type == "text")
    return body, bool(result.isError), dict(result.meta or {})


TOOLS = {
    "wait_for_events": dict(after_seq=0, timeout=0),
    "wait_for_new_message": dict(timeout=0),
    "wait_for_settled_message": dict(max_wait_ms=0),
    "get_messages_after": {},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", sorted(TOOLS))
@pytest.mark.parametrize("allowed", [str(CHANNEL), str(GROUP), f"{CHANNEL},{GROUP}"])
async def test_alias_never_looks_up_marked_variants_in_any_waiter(monkeypatch, tool, allowed):
    stub = setup(monkeypatch, allowed, {CHANNEL, GROUP})
    for chat in (WORDING, runtime.AliasID(ID, WORDING)):
        body, _, _ = await call(tool, chat_id=chat, **TOOLS[tool])
        assert stub.marked() == []
        assert "events" not in body or '"chat_id": %d' % ID not in body


@pytest.mark.asyncio
async def test_get_messages_after_plain_int_keeps_marked_fallback(monkeypatch):
    stub = setup(monkeypatch, None, {CHANNEL})
    body, is_error, _ = await call("get_messages_after", chat_id=ID)
    assert not is_error and stub.lookups == [ID, ID, CHANNEL]
    digits = setup(monkeypatch, None, {CHANNEL})
    await call("get_messages_after", chat_id=str(ID))
    assert digits.lookups == [ID, ID, CHANNEL]


@pytest.mark.asyncio
async def test_get_messages_after_alias_is_exact_without_allowlist(monkeypatch):
    stub = setup(monkeypatch, None, {CHANNEL, GROUP})
    body, _, _ = await call("get_messages_after", chat_id=WORDING)
    assert stub.marked() == [] and "supergroup" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", sorted(TOOLS))
@pytest.mark.parametrize(
    "chat", [WORDING, ID, str(ID), "@stranger"], ids="alias int digits user".split()
)
@pytest.mark.parametrize("allowed", [None, str(CHANNEL), str(ID)])
async def test_structured_errors_flag_changes_only_the_envelope(monkeypatch, tool, chat, allowed):
    """Flag on: same text and same lookups; a failure carries a kind, a success none."""
    off = setup(monkeypatch, allowed, {CHANNEL})
    plain_body, plain_err, plain_meta = await call(tool, chat_id=chat, **TOOLS[tool])
    assert not plain_err and plain_meta == {}
    off_lookups = list(off.lookups)

    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    on = setup(monkeypatch, allowed, {CHANNEL})
    body, is_error, meta = await call(tool, chat_id=chat, **TOOLS[tool])
    assert on.lookups == off_lookups
    if "boot" in plain_body:  # event feeds carry a per-process id
        assert not is_error and meta == {}
        return
    assert body == plain_body
    if is_error:
        assert meta["telegram-mcp/error"]["kind"] in {
            "forbidden_allowlist",
            "invalid_argument",
            "not_found",
        }
    else:
        assert meta == {}


@pytest.mark.asyncio
async def test_structured_refusal_kinds_by_input(monkeypatch):
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    setup(monkeypatch, str(CHANNEL), {CHANNEL})
    kinds = {}
    for label, chat in (("int", ID), ("digits", str(ID)), ("user", "@stranger")):
        _, is_error, meta = await call("wait_for_events", chat_id=chat, **TOOLS["wait_for_events"])
        kinds[label] = (is_error, meta["telegram-mcp/error"]["kind"])
    assert kinds == {"int": (True, "forbidden_allowlist")} | {
        "digits": (True, "forbidden_allowlist"),
        "user": (True, "forbidden_allowlist"),
    }
