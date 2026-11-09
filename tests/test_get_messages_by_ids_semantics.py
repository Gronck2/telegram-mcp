"""get_messages_by_ids (task 0009): real Telethon _IDsIter semantics, allowlist variants,
MCP-boundary validation and output regressions. Nothing here touches real Telegram."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl import functions, types

from telegram_mcp import runtime
from telegram_mcp.tools import messages

DATE = datetime(2026, 10, 5, tzinfo=timezone.utc)
CHANNEL_ID = 1234567890
CHAT = -1001234567890
META = "telegram-mcp/error"


def _chan_msg(msg_id, text=None):
    return types.Message(
        id=msg_id,
        peer_id=types.PeerChannel(CHANNEL_ID),
        date=DATE,
        message=f"m{msg_id}" if text is None else text,
    )


def _user_msg(msg_id, peer_user):
    return types.Message(
        id=msg_id, peer_id=types.PeerUser(peer_user), date=DATE, message=f"u{msg_id}"
    )


def _channel():
    return types.Channel(
        id=CHANNEL_ID,
        title="Chan",
        photo=types.ChatPhotoEmpty(),
        date=DATE,
        access_hash=7,
        megagroup=True,
    )


class RealIterClient(TelegramClient):
    """A genuine Telethon TelegramClient (so the real _IDsIter runs); only the network
    call and peer lookup are replaced."""

    def __init__(self, handler, input_entity, peer):
        super().__init__(StringSession(), 1, "0" * 32)
        self._handler = handler
        self._input_entity = input_entity
        self._peer = peer
        self.requests = []

    async def __call__(self, request, ordered=False, flood_sleep_threshold=None):
        self.requests.append(request)
        return self._handler(request)

    async def get_input_entity(self, peer):
        return self._input_entity

    async def _get_peer(self, peer):
        return self._peer


def _channel_client(existing, raise_empty_for_all_missing=True):
    """Channel peer: Telegram omits absent ids from the answer (misaligned list)."""

    def handler(request):
        assert isinstance(request, functions.channels.GetMessagesRequest)
        found = [_chan_msg(i) for i in request.id if i in existing]
        if not found and raise_empty_for_all_missing:
            raise errors.MessageIdsEmptyError(request)
        return types.messages.ChannelMessages(
            pts=1, count=len(found), messages=found, topics=[], chats=[], users=[]
        )

    return RealIterClient(
        handler, types.InputPeerChannel(CHANNEL_ID, 7), types.PeerChannel(CHANNEL_ID)
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(
        messages, "resolve_entity", AsyncMock(return_value=_channel()), raising=True
    )
    monkeypatch.setattr(messages, "ensure_connected", AsyncMock())
    monkeypatch.setattr(messages.transcription, "prefetch_transcripts", AsyncMock())
    monkeypatch.delenv("TELEGRAM_STRUCTURED_ERRORS", raising=False)
    for var in ("TELEGRAM_ALLOWED_CHAT_IDS", "TELEGRAM_ALLOWED_READ_CHATS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", None)


def _use(monkeypatch, client):
    monkeypatch.setattr(messages, "get_client", lambda account=None: client)
    monkeypatch.setattr(runtime, "clients", {"one": client})
    return client


async def _run(ids):
    out = await messages.get_messages_by_ids(chat_id=CHAT, message_ids=ids, account="one")
    return json.loads(out)


# --- 1. real Telethon semantics ------------------------------------------------------


@pytest.mark.asyncio
async def test_real_iter_channel_omitted_ids_are_matched_by_id(monkeypatch):
    # Telegram skips absent ids: the raw list is shorter than the request.
    cl = _use(monkeypatch, _channel_client({10, 30}))
    raw = await cl.get_messages(_channel(), ids=[30, 20, 10])
    assert [m.id for m in raw] == [30, 10]  # confirms the misalignment is real
    data = await _run([30, 20, 10])
    assert [r["id"] for r in data["results"]] == [30, 10]
    assert data["missing_ids"] == [20]


@pytest.mark.asyncio
async def test_real_iter_channel_all_missing_message_ids_empty_error(monkeypatch):
    cl = _use(monkeypatch, _channel_client(set()))
    data = await _run([5, 6])
    assert data == {"results": [], "missing_ids": [5, 6]}
    assert len(cl.requests) == 1


@pytest.mark.asyncio
async def test_real_iter_message_empty_in_channel_answer(monkeypatch):
    def handler(request):
        msgs = [_chan_msg(1), types.MessageEmpty(id=2, peer_id=types.PeerChannel(CHANNEL_ID))]
        return types.messages.ChannelMessages(
            pts=1, count=2, messages=msgs, topics=[], chats=[], users=[]
        )

    cl = RealIterClient(
        handler, types.InputPeerChannel(CHANNEL_ID, 7), types.PeerChannel(CHANNEL_ID)
    )
    _use(monkeypatch, cl)
    data = await _run([1, 2])
    assert [r["id"] for r in data["results"]] == [1]
    assert data["missing_ids"] == [2]


@pytest.mark.asyncio
async def test_real_iter_private_chat_foreign_and_empty_ids(monkeypatch):
    # Non-channel peer: GetMessages(ids) is global; ids of another chat come back as
    # None (from_id check) and MessageEmpty is dropped, so the list is misaligned.
    def handler(request):
        assert isinstance(request, functions.messages.GetMessagesRequest)
        msgs = [
            _user_msg(1, 111),
            _user_msg(2, 222),  # belongs to another chat
            types.MessageEmpty(id=3, peer_id=types.PeerUser(111)),
            _user_msg(4, 111),
        ]
        return types.messages.Messages(messages=msgs, topics=[], chats=[], users=[])

    cl = RealIterClient(handler, types.InputPeerUser(111, 1), types.PeerUser(111))
    _use(monkeypatch, cl)
    monkeypatch.setattr(
        messages,
        "resolve_entity",
        AsyncMock(return_value=types.User(id=111, access_hash=1, first_name="U")),
    )
    data = await _run([1, 2, 3, 4])
    assert [r["id"] for r in data["results"]] == [1, 4]
    assert data["missing_ids"] == [2, 3]


@pytest.mark.asyncio
async def test_real_iter_chunks_by_100_cap_is_one_request(monkeypatch):
    cl = _use(monkeypatch, _channel_client(set(range(1, 101))))
    data = await _run(list(range(1, 101)))
    assert len(cl.requests) == 1
    assert len(data["results"]) == 100 and data["missing_ids"] == []
    assert [r["id"] for r in data["results"]] == list(range(1, 101))


# --- 2. allowlists ------------------------------------------------------------------


class _Stub:
    def __init__(self, store):
        self.store = {m.id: m for m in store}
        self.calls = []

    def is_connected(self):
        return True

    async def __call__(self, request):
        return None

    async def get_dialogs(self, *a, **k):
        return []

    async def get_entity(self, key):
        return _channel()

    async def get_input_entity(self, key):
        return _channel()

    async def get_messages(self, entity, ids=None, **kw):
        self.calls.append(ids)
        return [self.store.get(i) for i in ids]


async def _call(name="get_messages_by_ids", **arguments):
    request = CallToolRequest(
        method="tools/call", params=CallToolRequestParams(name=name, arguments=arguments)
    )
    return (await runtime.mcp._mcp_server.request_handlers[CallToolRequest](request)).root


def _real_path(monkeypatch, stub):
    monkeypatch.undo()
    monkeypatch.setattr(runtime, "clients", {"one": stub})
    monkeypatch.setattr(runtime, "_last_conn_verified", {})
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", None)
    for var in (
        "TELEGRAM_ALLOWED_CHAT_IDS",
        "TELEGRAM_ALLOWED_READ_CHATS",
        "TELEGRAM_ALLOWED_WRITE_CHATS",
        "TELEGRAM_STRUCTURED_ERRORS",
    ):
        monkeypatch.delenv(var, raising=False)


async def _pair(**arguments):
    """Same refusal check for get_messages_by_ids and the reference tool get_history."""
    new = await _call(chat_id=CHAT, account="one", **arguments)
    ref = await _call("get_history", chat_id=CHAT, account="one")
    return new, ref


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "var,value",
    [
        ("TELEGRAM_ALLOWED_READ_CHATS", "999"),
        ("TELEGRAM_ALLOWED_CHAT_IDS", "999"),
        ("TELEGRAM_ALLOWED_WRITE_CHATS", str(CHAT)),  # write-only list: chat not readable
        ("TELEGRAM_ALLOWED_WRITE_CHATS", "999"),
    ],
)
async def test_refusal_variants_match_get_history(monkeypatch, var, value):
    stub = _Stub([_chan_msg(1)])
    _real_path(monkeypatch, stub)
    monkeypatch.setenv(var, value)
    new, ref = await _pair(message_ids=[1])
    assert new.isError == ref.isError is False
    assert new.content[0].text == ref.content[0].text
    assert not new.content[0].text.startswith("{")
    assert stub.calls == []
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    new, ref = await _pair(message_ids=[1])
    assert new.isError and ref.isError
    assert new.meta == ref.meta == {META: {"kind": "forbidden_allowlist"}}
    assert new.content[0].text == ref.content[0].text
    assert stub.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "var,value",
    [
        ("TELEGRAM_ALLOWED_CHAT_IDS", str(CHAT)),
        ("TELEGRAM_ALLOWED_READ_CHATS", "*"),
        ("TELEGRAM_ALLOWED_READ_CHATS", str(CHAT)),
    ],
)
async def test_allowed_variants_work(monkeypatch, var, value):
    stub = _Stub([_chan_msg(1)])
    _real_path(monkeypatch, stub)
    monkeypatch.setenv(var, value)
    result = await _call(chat_id=CHAT, message_ids=[1, 2], account="one")
    assert not result.isError
    assert json.loads(result.content[0].text)["missing_ids"] == [2]
    assert stub.calls == [[1, 2]]


def test_read_only_mode_keeps_tool_exposed():
    names = {t.name for t in runtime.mcp._tool_manager.list_tools()}
    assert "get_messages_by_ids" in names

    class _Mgr:
        def __init__(self):
            self.removed = []

        def list_tools(self):
            return list(runtime.mcp._tool_manager.list_tools())

        def remove_tool(self, name):
            self.removed.append(name)

    class _Server:
        _tool_manager = _Mgr()

    result = runtime._apply_exposed_tools_mode(_Server, "read-only")
    assert "get_messages_by_ids" not in _Server._tool_manager.removed
    assert "get_messages_by_ids" not in result.hidden
    assert "send_message" in _Server._tool_manager.removed  # the filter did run


# --- 3. validation through the real MCP handler -----------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize(
    "bad",
    [7763, None, "abc", {"a": 1}, [[1, 2]], [1.5], ["a"], [None], [], [0], [-3]]
    + [list(range(1, 102))],
)
async def test_mcp_handler_rejects_bad_input_without_network(monkeypatch, bad, structured):
    stub = _Stub([_chan_msg(1)])
    _real_path(monkeypatch, stub)
    if structured:
        monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    result = await _call(chat_id=CHAT, message_ids=bad, account="one")
    assert stub.calls == []
    text = result.content[0].text
    assert "GEN-ERR" not in text and "Traceback" not in text
    if structured:
        assert result.isError
        assert result.meta == {META: {"kind": "invalid_argument"}}
    else:
        assert text.startswith(("Error", "Error executing tool"))


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("given", [["7763"], [1.0], [True], [2**31], [2**40]])
async def test_mcp_boundary_is_strict(monkeypatch, given, structured):
    """list[StrictInt]: no str/float/bool item coercion over MCP;
    ids above int32 are refused before any network call."""
    stub = _Stub([_chan_msg(1), _chan_msg(2), _chan_msg(7763)])
    _real_path(monkeypatch, stub)
    if structured:
        monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    result = await _call(chat_id=CHAT, message_ids=given, account="one")
    assert stub.calls == []
    if structured:
        assert result.isError
        assert result.meta == {META: {"kind": "invalid_argument"}}
    else:
        assert result.content[0].text.startswith(("Error", "Error executing tool"))


@pytest.mark.asyncio
async def test_mcp_json_string_argument_is_preparsed_by_fastmcp(monkeypatch):
    """Known limit: FastMCP's pre_parse_json turns the string '[1,2]' into a list before
    validation, and its items are real ints, so StrictInt accepts it. Preventing that needs
    runtime.py changes; pinned so a change is visible."""
    stub = _Stub([_chan_msg(1), _chan_msg(2)])
    _real_path(monkeypatch, stub)
    result = await _call(chat_id=CHAT, message_ids="[1,2]", account="one")
    assert not result.isError
    assert stub.calls == [[1, 2]]


@pytest.mark.asyncio
async def test_int32_upper_bound(monkeypatch):
    stub = _Stub([_chan_msg(2**31 - 1)])
    _real_path(monkeypatch, stub)
    result = await _call(chat_id=CHAT, message_ids=[2**31 - 1], account="one")
    assert not result.isError
    assert stub.calls == [[2**31 - 1]]
    assert json.loads(result.content[0].text)["missing_ids"] == []
    # direct call: refused pre-network as invalid_argument
    out = await messages.get_messages_by_ids(chat_id=CHAT, message_ids=[2**31], account="one")
    assert out.startswith("Error") and "2147483647" in out
    assert stub.calls == [[2**31 - 1]]


# --- 4. output --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_records_identical_to_get_history_and_dedupe_order(monkeypatch):
    msgs = [_chan_msg(4, "a\x00b <x>"), _chan_msg(9, "y" * 5000), _chan_msg(6)]
    by_id = {m.id: m for m in msgs}
    cl = _use(monkeypatch, _channel_client(set()))

    async def fetch(entity, ids=None, **kw):
        return [by_id.get(i) for i in ids]

    cl.get_messages = fetch
    data = await _run([9, 4, 9, 6, 4])
    assert [r["id"] for r in data["results"]] == [9, 4, 6]
    assert data["missing_ids"] == []
    assert data["results"][0].get("truncated") is True
    assert "\x00" not in data["results"][1]["text"]
    cl.get_messages = AsyncMock(return_value=[by_id[i] for i in (9, 4, 6)])
    history = json.loads(await messages.get_history(chat_id=CHAT, account="one"))
    assert data["results"] == history["results"]


@pytest.mark.asyncio
async def test_missing_ids_always_present_and_all_ints(monkeypatch):
    _use(monkeypatch, _channel_client({1}))
    for ids in ([1], [1, 2], [2, 3]):
        data = await _run(ids)
        assert "missing_ids" in data
        assert all(type(i) is int for i in data["missing_ids"])
    assert (await _run([2, 3]))["results"] == []


@pytest.mark.asyncio
async def test_all_missing_structured_errors_not_error(monkeypatch):
    stub = _Stub([])
    _real_path(monkeypatch, stub)
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    result = await _call(chat_id=CHAT, message_ids=[3, 2], account="one")
    assert not result.isError and result.meta is None
    assert json.loads(result.content[0].text) == {"results": [], "missing_ids": [3, 2]}


@pytest.mark.asyncio
async def test_existing_tool_output_unchanged_by_new_tool(monkeypatch):
    """get_history keeps its {results, ...} shape without any missing_ids key."""
    cl = _use(monkeypatch, _channel_client({1}))
    cl.get_messages = AsyncMock(return_value=[_chan_msg(1)])
    out = json.loads(await messages.get_history(chat_id=CHAT, account="one"))
    assert "missing_ids" not in out
    assert out["results"][0]["id"] == 1
