"""Alias-derived ids through real tools and the allowlist (task 0010).

Stub clients only. Every case has an alias whose id has no entity while the marked
variants (-100<id> channel, -<id> basic group) do exist.
"""

import json

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams
from telethon import types

from telegram_mcp import runtime
from telegram_mcp.tools import chats, groups, messages  # also registers the real tools

ID = 7654321
OTHER_ID = 7654322
CHANNEL = -1000000000000 - ID
GROUP = -ID
WORDING = "мой контакт"
OTHER_WORDING = "другой контакт"
MARKED = (CHANNEL, GROUP)


class Recorder:
    """Stub client: knows only ``known`` ids; records lookups and any request sent."""

    def __init__(self, known=()):
        self.known = set(known)
        self.lookups = []
        self.requests = []
        self.sent = []

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

    async def __call__(self, request, *args, **kwargs):
        self.requests.append(request)
        return None

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))

    def iter_participants(self, *args, **kwargs):
        self.requests.append(("iter_participants", args))
        raise AssertionError("must not list participants of a guessed chat")

    def iter_messages(self, *args, **kwargs):
        self.requests.append(("iter_messages", args))
        raise AssertionError("must not read history of a guessed chat")

    def touched_marked(self):
        text = repr(self.requests) + repr(self.sent)
        return [m for m in MARKED if m in self.lookups or str(m) in text]


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
    runtime.save_aliases({WORDING: ID, OTHER_WORDING: OTHER_ID})

    async def connected(client=None):
        return None

    monkeypatch.setattr(runtime, "ensure_connected", connected)
    # tool modules hold their own reference (star import)
    for module in (groups, messages, chats):
        monkeypatch.setattr(module, "ensure_connected", connected, raising=False)


def use(monkeypatch, client):
    monkeypatch.setattr(runtime, "clients", {"one": client})
    return client


async def call(name, **arguments):
    request = CallToolRequest(
        method="tools/call", params=CallToolRequestParams(name=name, arguments=arguments)
    )
    return (await runtime.mcp._mcp_server.request_handlers[CallToolRequest](request)).root


def text(result):
    return "\n".join(b.text for b in result.content if b.type == "text")


def parse(body):
    """Tool errors raised as ToolError arrive prefixed with 'Error executing tool ...: '."""
    return json.loads(body[body.index("{") :])


def allow(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", None)


def assert_stale(body, wording=WORDING, stored=ID):
    payload = parse(body)
    assert payload["error"] == "stale_contact"
    assert wording in payload["instruction"] and str(stored) in payload["instruction"]


TOOLS = [
    ("get_chat", {}),
    ("get_history", {}),
    ("send_message", {"message": "hello"}),
    ("get_participants", {}),
]
TOOL_IDS = [name for name, _ in TOOLS]


# --- 1. real tools ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name,extra", TOOLS, ids=TOOL_IDS)
@pytest.mark.parametrize("by", ["wording", "plain-id"])
async def test_real_tool_stale_alias_never_touches_marked_chat(monkeypatch, name, extra, by):
    client = use(monkeypatch, Recorder({CHANNEL, GROUP}))
    chat = WORDING if by == "wording" else ID
    result = await call(name, chat_id=chat, **extra)
    body = text(result)
    if by == "wording":
        assert_stale(body)
    else:
        # a plain id typed by the user keeps its marked fallback (not an alias)
        assert client.lookups[:2] == [ID, ID]
        return
    assert client.touched_marked() == []
    assert client.lookups == [ID, ID]
    assert client.sent == [] and client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name,extra", TOOLS, ids=TOOL_IDS)
async def test_real_tool_structured_stale_alias(monkeypatch, name, extra):
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    client = use(monkeypatch, Recorder({CHANNEL, GROUP}))
    result = await call(name, chat_id=WORDING, **extra)
    assert_stale(text(result))
    assert result.isError is True
    assert client.touched_marked() == []
    assert client.requests == [] and client.sent == []


@pytest.mark.asyncio
async def test_real_tool_alias_in_list_param_never_guesses(monkeypatch):
    client = use(monkeypatch, Recorder({CHANNEL, GROUP, 55}))
    result = await call("invite_to_group", group_id=55, user_ids=[WORDING])
    assert client.touched_marked() == []
    assert not any(r for r in client.requests if "InviteTo" in repr(r))
    assert text(result)


@pytest.mark.asyncio
async def test_list_param_substitutes_alias_ids_for_every_item(monkeypatch):
    seen = []

    @runtime.mcp.tool(name="zz_task0010_list")
    @runtime.with_account(readonly=True)
    @runtime.validate_id("targets")
    async def tool(targets: list, account: str = None) -> str:
        seen.extend(targets)
        client = runtime.get_client(account)
        out = []
        for item in targets:
            try:
                out.append(await runtime.resolve_entity(item, client))
            except runtime.AliasNeedsUser as error:
                out.append(json.loads(error.payload)["error"])
        return json.dumps(out)

    try:
        client = use(monkeypatch, Recorder({CHANNEL, GROUP, OTHER_ID}))
        result = await call("zz_task0010_list", targets=[WORDING, OTHER_WORDING, str(ID)])
    finally:
        runtime.mcp._tool_manager._tools.pop("zz_task0010_list", None)
    assert all(isinstance(item, runtime.AliasID) for item in seen[:2])
    assert not isinstance(seen[2], runtime.AliasID)
    # alias -> stale question; other alias -> its own id; plain digit string -> fallback
    assert json.loads(text(result)) == ["stale_contact", f"entity:{OTHER_ID}", f"entity:{CHANNEL}"]
    # the only marked lookup is the one for the plain digit string
    assert client.lookups.count(CHANNEL) == 1


# --- 2. allowlist interplay -------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name,extra", TOOLS, ids=TOOL_IDS)
async def test_allowlisted_alias_id_unlisted_marked_chat_never_used(monkeypatch, name, extra):
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS=str(ID))
    client = use(monkeypatch, Recorder({CHANNEL, GROUP}))
    result = await call(name, chat_id=WORDING, **extra)
    assert_stale(text(result))
    assert client.touched_marked() == []
    assert client.requests == [] and client.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name,extra", TOOLS, ids=TOOL_IDS)
async def test_marked_chat_allowed_alias_id_not_is_treated_as_unsaved(monkeypatch, name, extra):
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS=f"{CHANNEL},{GROUP}")
    client = use(monkeypatch, Recorder({CHANNEL, GROUP}))
    result = await call(name, chat_id=WORDING, **extra)
    payload = parse(text(result))
    # not allowed by either list: the nickname is as good as unsaved, never resolved
    assert payload["error"] != "stale_contact"
    assert str(ID) not in text(result)
    assert client.lookups == []
    assert client.touched_marked() == []
    assert client.requests == [] and client.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name,extra", TOOLS, ids=TOOL_IDS)
async def test_marked_chat_allowed_plain_positive_id_denied(monkeypatch, name, extra):
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS=f"{CHANNEL},{GROUP}")
    client = use(monkeypatch, Recorder({CHANNEL, GROUP}))
    result = await call(name, chat_id=ID, **extra)
    body = text(result)
    assert "restricted by privacy policy" in body
    assert client.lookups == [] and client.requests == [] and client.sent == []


@pytest.mark.asyncio
async def test_write_only_allowlist_alias_stale_does_not_send(monkeypatch):
    allow(
        monkeypatch,
        TELEGRAM_ALLOWED_READ_CHATS=str(OTHER_ID),
        TELEGRAM_ALLOWED_WRITE_CHATS=str(ID),
    )
    client = use(monkeypatch, Recorder({CHANNEL, GROUP}))
    result = await call("send_message", chat_id=WORDING, message="hello")
    assert_stale(text(result))
    assert client.touched_marked() == [] and client.sent == [] and client.requests == []


@pytest.mark.asyncio
async def test_read_only_allowlisted_alias_cannot_write_and_never_guesses(monkeypatch):
    allow(
        monkeypatch,
        TELEGRAM_ALLOWED_READ_CHATS=str(ID),
        TELEGRAM_ALLOWED_WRITE_CHATS=str(OTHER_ID),
    )
    client = use(monkeypatch, Recorder({CHANNEL, GROUP}))
    result = await call("send_message", chat_id=WORDING, message="hello")
    body = text(result)
    assert "restricted" in body or parse(body)["error"]
    assert client.touched_marked() == [] and client.sent == [] and client.requests == []


# --- 3. AliasID identity ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_alias_id_survives_with_account_fan_out_to_the_resolver(monkeypatch):
    received = {}

    @runtime.mcp.tool(name="zz_task0010_ident")
    @runtime.with_account(readonly=True)
    @runtime.validate_id("chat_id")
    async def tool(chat_id, account: str = None) -> str:
        received[account] = chat_id
        return "ok"

    try:
        a, b = Recorder(), Recorder()
        monkeypatch.setattr(runtime, "clients", {"a": a, "b": b})
        await call("zz_task0010_ident", chat_id=WORDING)
        assert set(received) == {"a", "b"}
        for value in received.values():
            assert type(value) is runtime.AliasID and value == ID and value.wording == WORDING
        received.clear()
        await call("zz_task0010_ident", chat_id=WORDING, account="a")
        assert type(received["a"]) is runtime.AliasID
    finally:
        runtime.mcp._tool_manager._tools.pop("zz_task0010_ident", None)


@pytest.mark.asyncio
async def test_fan_out_each_account_stays_exact(monkeypatch):
    a = Recorder({ID, CHANNEL})
    b = Recorder({CHANNEL, GROUP})
    monkeypatch.setattr(runtime, "clients", {"a": a, "b": b})
    result = await call("get_history", chat_id=WORDING)
    body = text(result)
    assert "stale_contact" in body
    assert b.touched_marked() == []
    assert a.lookups[:1] == [ID]
    assert CHANNEL not in a.lookups


@pytest.mark.asyncio
async def test_string_alias_and_alias_id_resolve_identically():
    for subject in (WORDING, runtime.AliasID(ID, WORDING)):
        client = Recorder({CHANNEL, GROUP})
        with pytest.raises(runtime.AliasNeedsUser):
            await runtime.resolve_entity(subject, client)
        assert client.lookups == [ID, ID]


@pytest.mark.asyncio
async def test_plain_int_equal_to_alias_id_is_not_treated_as_alias():
    client = Recorder({CHANNEL})
    assert await runtime.resolve_entity(ID, client) == f"entity:{CHANNEL}"
    client = Recorder({CHANNEL})
    assert await runtime.resolve_input_entity(str(ID), client) == f"entity:{CHANNEL}"


# --- 4. regression ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("plain", [ID, str(ID), f" {ID} "], ids=["int", "digits", "padded"])
async def test_plain_ids_keep_group_fallback_too(plain):
    client = Recorder({GROUP})
    assert await runtime.resolve_entity(plain, client) == f"entity:{GROUP}"
    assert client.lookups == [ID, ID, CHANNEL, GROUP]


@pytest.mark.asyncio
@pytest.mark.parametrize("plain", [ID, str(ID)], ids=["int", "digits"])
@pytest.mark.parametrize("getter", ["resolve_entity", "resolve_input_entity"])
async def test_plain_id_not_found_text_is_unchanged(plain, getter):
    client = Recorder()
    with pytest.raises(runtime.EntityNotFoundError) as error:
        await getattr(runtime, getter)(plain, client)
    label = "entity" if getter == "resolve_entity" else "input entity"
    assert str(error.value) == (
        f"Could not resolve {label} for {ID!r}, including marked variants [{CHANNEL}, {GROUP}]"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True])
async def test_plain_id_not_found_kind_unchanged_through_tool(monkeypatch, structured):
    if structured:
        monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    client = use(monkeypatch, Recorder())
    result = await call("get_chat", chat_id=ID)
    assert client.lookups == [ID, ID, CHANNEL, GROUP]
    if structured:
        assert result.isError is True
        assert result.meta["telegram-mcp/error"]["kind"] == "not_found"
    else:
        assert result.meta is None


@pytest.mark.asyncio
async def test_negative_and_username_references_unaffected():
    client = Recorder({CHANNEL, "someone"})
    assert await runtime.resolve_entity(CHANNEL, client) == f"entity:{CHANNEL}"
    assert await runtime.resolve_entity("someone", client) == "entity:someone"


# --- 4. saving a new alias against an existing alias ---------------------------------------


class UserRecorder(Recorder):
    """Like Recorder, but known ids resolve to real Telethon users."""

    def _look(self, identifier):
        self.lookups.append(identifier)
        if identifier in self.known:
            return types.User(id=identifier, first_name="Real", access_hash=1)
        raise ValueError(f"Could not find the input entity for {identifier!r}")


@pytest.mark.asyncio
async def test_set_contact_alias_stale_existing_alias_saves_nothing(monkeypatch):
    from telegram_mcp.tools import contacts

    client = use(monkeypatch, UserRecorder({CHANNEL, GROUP}))
    before = runtime.load_aliases()
    out = json.loads(await contacts.set_contact_alias("новый ник", WORDING))["results"]
    assert out["saved"] is False
    assert out["reason"] == "target_not_found"
    assert client.touched_marked() == []
    assert runtime.load_aliases() == before


@pytest.mark.asyncio
async def test_set_contact_alias_plain_digit_target_keeps_fallback(monkeypatch):
    from telegram_mcp.tools import contacts

    client = use(monkeypatch, UserRecorder({GROUP}))
    out = json.loads(await contacts.set_contact_alias("новый ник", str(ID)))["results"]
    assert out["saved"] is True
    assert GROUP in client.lookups


@pytest.mark.asyncio
async def test_set_contact_alias_valid_existing_alias_saves(monkeypatch):
    from telegram_mcp.tools import contacts

    use(monkeypatch, UserRecorder({ID}))
    out = json.loads(await contacts.set_contact_alias("новый ник", WORDING))["results"]
    assert out["saved"] is True
    assert any(rec["id"] == ID for rec in runtime.load_aliases().values())
