"""Alias-derived ids in the event waiters stay exact (task 0012).

Stub clients only. The alias points at a positive id N with no entity while the marked
variants (-100N channel, -N basic group) exist. The handler path already receives an
AliasID from @validate_id; the race-simulation tests call the decorated body directly
(``__wrapped__``) with the alias wording or a plain int, as a decorator/body race would.
"""

import json
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
MARKED = (CHANNEL, GROUP)


class Recorder:
    def __init__(self, known=()):
        self.known = set(known)
        self.lookups = []

    async def get_dialogs(self):
        return []

    async def get_entity(self, identifier):
        return self._look(identifier)

    async def get_input_entity(self, identifier):
        return self._look(identifier)

    def _look(self, identifier):
        self.lookups.append(identifier)
        if identifier in self.known:
            return f"entity:{identifier}"
        raise ValueError(f"Could not find the input entity for {identifier!r}")

    def touched_marked(self):
        return [m for m in MARKED if m in self.lookups]


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for name in (
        "TELEGRAM_ALLOWED_CHAT_IDS",
        "TELEGRAM_ALLOWED_READ_CHATS",
        "TELEGRAM_ALLOWED_WRITE_CHATS",
        "TELEGRAM_STRUCTURED_ERRORS",
        "TELEGRAM_EVENT_LOG_SIZE",
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


def use(monkeypatch, *clients):
    labels = {"one": clients[0]} if len(clients) == 1 else dict(zip("ab", clients))
    monkeypatch.setattr(runtime, "clients", labels)
    return clients[0]


def allow(monkeypatch, value):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", value)
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", None)


def seed(*chat_ids):
    for chat_id in chat_ids:
        events._append_event({"chat_id": chat_id, "message_id": 1, "chat_type": "private"})


async def call(name, **arguments):
    request = CallToolRequest(
        method="tools/call", params=CallToolRequestParams(name=name, arguments=arguments)
    )
    return (await runtime.mcp._mcp_server.request_handlers[CallToolRequest](request)).root


def text(result):
    return "\n".join(b.text for b in result.content if b.type == "text")


def event_chats(body):
    return [e["chat_id"] for e in json.loads(body)["events"]]


def bare(tool):
    """The tool body without the @validate_id / @with_account wrappers."""
    while hasattr(tool, "__wrapped__"):
        tool = tool.__wrapped__
    return tool


def wait_quick(**extra):
    return dict(after_seq=0, timeout=0, **extra)


# --- helper ---------------------------------------------------------------------------


def test_exact_alias_wraps_only_alias_strings():
    got = events._exact_alias(WORDING)
    assert type(got) is runtime.AliasID and got == ID and got.wording == WORDING
    already = runtime.AliasID(ID, WORDING)
    assert events._exact_alias(already) is already
    assert type(events._exact_alias(ID)) is int
    assert events._exact_alias("@someone") == "@someone"
    assert events._exact_alias("unknown nickname") == "unknown nickname"


@pytest.mark.asyncio
async def test_wait_target_alias_string_is_exact_and_makes_no_lookup(monkeypatch):
    client = use(monkeypatch, Recorder({CHANNEL, GROUP}))
    target = await events._wait_target(WORDING)
    assert type(target) is runtime.AliasID and target == ID
    assert client.lookups == []
    assert await events._wait_target(ID) == ID
    assert await events._wait_target(None) is None and await events._wait_target("") is None


# --- wait_for_events through the real handler ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [str(CHANNEL), str(GROUP), f"{CHANNEL},{GROUP}"])
async def test_handler_alias_not_allowed_is_refused_without_marked_lookup(monkeypatch, allowed):
    allow(monkeypatch, allowed)
    client = use(monkeypatch, Recorder({CHANNEL, GROUP}))
    seed(CHANNEL, GROUP, ID)
    result = await call("wait_for_events", chat_id=WORDING, **wait_quick())
    body = text(result)
    assert "events" not in body or event_chats(body) == []
    assert str(ID) not in body
    assert client.touched_marked() == []
    # same answer as for a nickname that is not saved at all
    unknown = text(await call("wait_for_events", chat_id="no such nickname", **wait_quick()))
    assert ("events" in body) == ("events" in unknown)


@pytest.mark.asyncio
async def test_handler_alias_allowed_exactly_returns_its_events(monkeypatch):
    allow(monkeypatch, str(ID))
    client = use(monkeypatch, Recorder({CHANNEL, GROUP}))
    seed(CHANNEL, ID, GROUP)
    body = text(await call("wait_for_events", chat_id=WORDING, **wait_quick()))
    assert event_chats(body) == [ID]
    assert client.lookups == []


@pytest.mark.asyncio
async def test_handler_alias_allowlist_off_returns_its_events_without_lookup(monkeypatch):
    client = use(monkeypatch, Recorder({CHANNEL, GROUP}))
    seed(CHANNEL, ID, GROUP)
    body = text(await call("wait_for_events", chat_id=WORDING, **wait_quick()))
    assert event_chats(body) == [ID]
    assert client.lookups == []


@pytest.mark.asyncio
async def test_handler_alias_with_account_and_fan_out_stays_exact(monkeypatch):
    allow(monkeypatch, str(CHANNEL))
    a, b = Recorder({CHANNEL, GROUP}), Recorder({CHANNEL, GROUP})
    use(monkeypatch, a, b)
    seed(CHANNEL, ID)
    for extra in ({"account": "a"}, {}):
        body = text(await call("wait_for_events", chat_id=WORDING, **wait_quick(), **extra))
        assert ID not in event_chats(body) if "events" in body else True
    assert a.touched_marked() == [] and b.touched_marked() == []


# --- race simulation: decorator skipped, alias wording reaches the body ------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [str(CHANNEL), str(GROUP)])
async def test_body_alias_wording_never_guesses_marked_variants(monkeypatch, allowed):
    allow(monkeypatch, allowed)
    client = use(monkeypatch, Recorder({CHANNEL, GROUP}))
    seed(CHANNEL, GROUP, ID)
    body = await bare(events.wait_for_events)(chat_id=WORDING, **wait_quick())
    assert client.touched_marked() == []
    assert "events" not in body or event_chats(body) == []


@pytest.mark.asyncio
async def test_body_alias_wording_allowed_exactly_works(monkeypatch):
    allow(monkeypatch, str(ID))
    client = use(monkeypatch, Recorder({CHANNEL, GROUP}))
    seed(CHANNEL, ID)
    body = await bare(events.wait_for_events)(chat_id=WORDING, **wait_quick())
    assert event_chats(body) == [ID]
    assert client.lookups == []


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [str(CHANNEL), str(GROUP), "@friend"])
async def test_race_allowlist_changes_between_decorator_and_pre_check(monkeypatch, allowed):
    """apply_alias hands out the id, then the allowlist no longer covers it: the
    pre-check looks the chat up, and that lookup must be exact."""
    allow(monkeypatch, allowed)
    monkeypatch.setattr(events, "apply_alias", lambda x: ID if x == WORDING else x)
    client = use(monkeypatch, Recorder({CHANNEL, GROUP}))
    seed(CHANNEL, GROUP, ID)
    body = await bare(events.wait_for_events)(chat_id=WORDING, **wait_quick())
    assert client.touched_marked() == []
    assert "stale_contact" in body or "restricted" in body
    assert "events" not in body or event_chats(body) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["wait_for_new_message", "wait_for_settled_message"])
@pytest.mark.parametrize("path", ["handler", "body"])
async def test_other_waiters_share_the_exact_target(monkeypatch, tool, path):
    allow(monkeypatch, str(ID))
    client = use(monkeypatch, Recorder({CHANNEL, GROUP}))
    kwargs = {"timeout": 0} if tool == "wait_for_new_message" else {"max_wait_ms": 0}
    if path == "handler":
        body = text(await call(tool, chat_id=WORDING, **kwargs))
    else:
        body = await bare(getattr(events, tool))(chat_id=WORDING, **kwargs)
    assert client.lookups == []
    if tool == "wait_for_settled_message":
        assert json.loads(body)["waiting_for"] == ID


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["wait_for_new_message", "wait_for_settled_message"])
async def test_other_waiters_target_is_exact_alias_id(monkeypatch, tool):
    seen = []
    real = events._wait_target

    async def spy(chat_id, account=None):
        seen.append(await real(chat_id, account))
        return seen[-1]

    monkeypatch.setattr(events, "_wait_target", spy)
    use(monkeypatch, Recorder({CHANNEL, GROUP}))
    kwargs = {"timeout": 0} if tool == "wait_for_new_message" else {"max_wait_ms": 0}
    await bare(getattr(events, tool))(chat_id=WORDING, **kwargs)
    assert type(seen[0]) is runtime.AliasID and seen[0] == ID


# --- plain inputs behave as on base ------------------------------------------------------


DENIED = (
    "Read access to chat '{ref}' is restricted by privacy policy "
    "(TELEGRAM_ALLOWED_CHAT_IDS / TELEGRAM_ALLOWED_READ_CHATS)."
)


@pytest.mark.asyncio
async def test_plain_int_keeps_marked_fallback_in_pre_check(monkeypatch):
    allow(monkeypatch, str(CHANNEL))
    client = use(monkeypatch, Recorder({CHANNEL}))
    body = await bare(events.wait_for_events)(chat_id=ID, **wait_quick())
    assert client.lookups == [ID, ID, CHANNEL]
    assert body == DENIED.format(ref=ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("plain", [ID, str(ID)], ids=["int", "digits"])
@pytest.mark.parametrize("allowed", [str(CHANNEL), f"{CHANNEL},{GROUP}"])
async def test_plain_int_and_digit_string_refusal_text(monkeypatch, plain, allowed):
    allow(monkeypatch, allowed)
    client = use(monkeypatch, Recorder({CHANNEL, GROUP}))
    body = text(await call("wait_for_events", chat_id=plain, **wait_quick()))
    assert body == DENIED.format(ref=ID)
    assert client.lookups == []


@pytest.mark.asyncio
async def test_username_refusal_unchanged(monkeypatch):
    allow(monkeypatch, str(CHANNEL))
    client = use(monkeypatch, Recorder())
    body = text(await call("wait_for_events", chat_id="@stranger", **wait_quick()))
    assert body == DENIED.format(ref="@stranger")
    assert client.lookups == ["@stranger", "@stranger"]


@pytest.mark.asyncio
async def test_plain_int_and_username_wait_without_allowlist(monkeypatch):
    client = use(monkeypatch, Recorder())
    seed(ID, CHANNEL)
    body = text(await call("wait_for_events", chat_id=ID, **wait_quick()))
    assert event_chats(body) == [ID]
    assert client.lookups == []


@pytest.mark.asyncio
async def test_username_target_resolves_through_the_entity(monkeypatch):
    class UserRecorder(Recorder):
        def _look(self, identifier):
            self.lookups.append(identifier)
            return types.User(id=42, first_name="Real", access_hash=1)

    client = use(monkeypatch, UserRecorder())
    target = await events._wait_target("@someone")
    assert target == 42 and type(target) is int
    assert client.lookups == ["@someone"]
