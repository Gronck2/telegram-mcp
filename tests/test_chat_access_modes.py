"""Separate read/write chat allowlists (#116).

TELEGRAM_ALLOWED_CHAT_IDS grants read and write access; TELEGRAM_ALLOWED_READ_CHATS
and TELEGRAM_ALLOWED_WRITE_CHATS grant one mode each. Once any of them is set,
both modes are restricted.
"""

import json
from types import SimpleNamespace

import pytest

import main
from telegram_mcp import runtime
from telegram_mcp.runtime import (
    CHAT_ACCESS_BOTH,
    CHAT_ACCESS_READ,
    CHAT_ACCESS_WRITE,
    chat_access_for_tool,
    check_chat_access,
    get_chat_allowlist,
    is_chat_allowed,
    is_chat_allowlist_enabled,
)
from telegram_mcp.tools import chats, media, messages

ENV_VARS = (
    "TELEGRAM_ALLOWED_CHAT_IDS",
    "TELEGRAM_ALLOWED_READ_CHATS",
    "TELEGRAM_ALLOWED_WRITE_CHATS",
)


@pytest.fixture(autouse=True)
def _clean_allowlists(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", None)
    monkeypatch.setattr(main, "ALLOWED_CHAT_IDS", None)


# ---------------------------------------------------------------------------
# Effective lists
# ---------------------------------------------------------------------------


def test_no_variables_means_unrestricted():
    assert not is_chat_allowlist_enabled()
    assert get_chat_allowlist(CHAT_ACCESS_READ) is None
    assert get_chat_allowlist(CHAT_ACCESS_WRITE) is None
    assert is_chat_allowed(42, access=CHAT_ACCESS_BOTH) is True


def test_read_list_alone_closes_writes(monkeypatch):
    """Setting only a read list must not leave writes open anywhere (fail closed)."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_READ_CHATS", "-100111")

    assert is_chat_allowlist_enabled()
    assert get_chat_allowlist(CHAT_ACCESS_READ) == {-100111}
    assert get_chat_allowlist(CHAT_ACCESS_WRITE) == set()
    assert is_chat_allowed(-100111, access=CHAT_ACCESS_READ) is True
    assert is_chat_allowed(-100111, access=CHAT_ACCESS_WRITE) is False
    assert is_chat_allowed(-100222, access=CHAT_ACCESS_READ) is False


def test_write_list_alone_closes_reads(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_WRITE_CHATS", "-100111")

    assert is_chat_allowed(-100111, access=CHAT_ACCESS_WRITE) is True
    assert is_chat_allowed(-100111, access=CHAT_ACCESS_READ) is False


def test_shared_list_grants_both_modes_and_merges(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    monkeypatch.setenv("TELEGRAM_ALLOWED_READ_CHATS", "2")
    monkeypatch.setenv("TELEGRAM_ALLOWED_WRITE_CHATS", "3")

    assert get_chat_allowlist(CHAT_ACCESS_READ) == {1, 2}
    assert get_chat_allowlist(CHAT_ACCESS_WRITE) == {1, 3}
    assert is_chat_allowed(1, access=CHAT_ACCESS_BOTH) is True
    assert is_chat_allowed(2, access=CHAT_ACCESS_BOTH) is False
    assert is_chat_allowed(3, access=CHAT_ACCESS_BOTH) is False


def test_legacy_module_attribute_still_counts_as_shared_list(monkeypatch):
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", {7})
    assert is_chat_allowed(7, access=CHAT_ACCESS_WRITE) is True
    assert is_chat_allowed(8, access=CHAT_ACCESS_READ) is False


def test_wildcard_read_with_narrow_write(monkeypatch):
    """Read everything, post only to one report chat."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_READ_CHATS", "*")
    monkeypatch.setenv("TELEGRAM_ALLOWED_WRITE_CHATS", "-100555")

    assert is_chat_allowed(123, access=CHAT_ACCESS_READ) is True
    assert is_chat_allowed("any_handle", access=CHAT_ACCESS_READ) is True
    assert is_chat_allowed(-100555, access=CHAT_ACCESS_WRITE) is True
    assert is_chat_allowed(123, access=CHAT_ACCESS_WRITE) is False


def test_usernames_and_entities_respect_mode(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_READ_CHATS", "@news")
    entity = SimpleNamespace(id=5, username="News")
    monkeypatch.setattr(runtime, "get_marked_id", lambda e: -1000000000005)

    assert is_chat_allowed("@news", access=CHAT_ACCESS_READ) is True
    assert is_chat_allowed(None, entity, CHAT_ACCESS_READ) is True
    assert is_chat_allowed(None, entity, CHAT_ACCESS_WRITE) is False


def test_unknown_access_mode_is_rejected():
    with pytest.raises(ValueError):
        get_chat_allowlist("admin")


def test_denial_message_names_the_mode_and_variable(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_READ_CHATS", "-100111")

    err = check_chat_access(-100111, access=CHAT_ACCESS_WRITE)
    assert "Write access to chat '-100111' is restricted by privacy policy" in err
    assert "TELEGRAM_ALLOWED_WRITE_CHATS" in err
    assert check_chat_access(-100111, access=CHAT_ACCESS_READ) is None
    both = check_chat_access(-100111, access=CHAT_ACCESS_BOTH)
    assert both.startswith("Write access")


# ---------------------------------------------------------------------------
# Tool classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name,param,expected",
    [
        ("get_messages", "chat_id", CHAT_ACCESS_READ),
        ("list_messages", "chat_id", CHAT_ACCESS_READ),
        ("wait_for_events", "chat_id", CHAT_ACCESS_READ),
        ("send_message", "chat_id", CHAT_ACCESS_WRITE),
        ("delete_message", "chat_id", CHAT_ACCESS_WRITE),
        ("promote_admin", "group_id", CHAT_ACCESS_WRITE),
        # Not read-only only because they write local files.
        ("download_media", "chat_id", CHAT_ACCESS_READ),
        ("export_chat_history", "chat_id", CHAT_ACCESS_READ),
        # forward reads the source chat and writes the destination.
        ("forward_message", "from_chat_id", CHAT_ACCESS_READ),
        ("forward_message", "to_chat_id", CHAT_ACCESS_WRITE),
        ("forward_messages", "from_chat_id", CHAT_ACCESS_READ),
        ("forward_messages", "to_chat_id", CHAT_ACCESS_WRITE),
        # Not a registered tool: must pass both lists.
        ("not_a_registered_tool", "chat_id", CHAT_ACCESS_BOTH),
    ],
)
def test_chat_access_for_tool(tool_name, param, expected):
    assert chat_access_for_tool(tool_name, param) == expected


def test_every_registered_chat_tool_has_a_definite_mode():
    """Each tool taking a chat parameter resolves to read or write via its annotations."""
    tools = runtime.mcp._tool_manager.list_tools()
    undetermined = [
        tool.name
        for tool in tools
        if set(tool.parameters.get("properties", {})) & runtime.CHAT_PARAM_NAMES
        and chat_access_for_tool(tool.name, "chat_id") == CHAT_ACCESS_BOTH
    ]
    assert undetermined == []


# ---------------------------------------------------------------------------
# Enforcement through real tools
# ---------------------------------------------------------------------------


def _unreachable_client(account=None):
    raise AssertionError("allowlist must reject before touching Telegram")


@pytest.mark.asyncio
async def test_send_message_needs_write_access(monkeypatch):
    monkeypatch.setattr(runtime, "get_client", _unreachable_client)
    monkeypatch.setenv("TELEGRAM_ALLOWED_READ_CHATS", "-100111")

    result = await messages.send_message(chat_id=-100111, message="hi")
    assert "Write access to chat '-100111' is restricted" in str(result)


@pytest.mark.asyncio
async def test_get_messages_needs_read_access(monkeypatch):
    monkeypatch.setattr(runtime, "get_client", _unreachable_client)
    monkeypatch.setenv("TELEGRAM_ALLOWED_WRITE_CHATS", "-100111")

    result = await messages.get_messages(chat_id=-100111)
    assert "Read access to chat '-100111' is restricted" in str(result)


@pytest.mark.asyncio
async def test_download_media_from_write_only_chat_is_denied(monkeypatch):
    monkeypatch.setattr(runtime, "get_client", _unreachable_client)
    monkeypatch.setenv("TELEGRAM_ALLOWED_WRITE_CHATS", "-100111")

    result = await media.download_media(chat_id=-100111, message_id=1)
    assert "Read access to chat '-100111' is restricted" in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "read,write,denied",
    [
        # Source only writable: reading it is what is refused.
        ("-100222", "-100111", "Read access to chat '-100111'"),
        # Destination only readable: writing to it is what is refused.
        ("-100111,-100222", "", "Write access to chat '-100222'"),
    ],
)
async def test_forward_checks_source_read_and_destination_write(monkeypatch, read, write, denied):
    monkeypatch.setattr(runtime, "get_client", _unreachable_client)
    monkeypatch.setenv("TELEGRAM_ALLOWED_READ_CHATS", read)
    monkeypatch.setenv("TELEGRAM_ALLOWED_WRITE_CHATS", write)

    result = await messages.forward_message(from_chat_id=-100111, message_id=1, to_chat_id=-100222)
    assert denied in str(result)


@pytest.mark.asyncio
async def test_forward_passes_gate_with_read_source_and_write_destination(monkeypatch):
    calls = []

    def _client(account=None):
        calls.append(account)
        raise RuntimeError("stop after the allowlist gate")

    monkeypatch.setattr(runtime, "get_client", _client)
    monkeypatch.setattr(messages, "get_client", _client)
    monkeypatch.setenv("TELEGRAM_ALLOWED_READ_CHATS", "-100111")
    monkeypatch.setenv("TELEGRAM_ALLOWED_WRITE_CHATS", "-100222")

    result = await messages.forward_message(from_chat_id=-100111, message_id=1, to_chat_id=-100222)
    assert "restricted by privacy policy" not in str(result)
    assert calls  # reached the tool body


class _Dialog:
    def __init__(self, entity):
        self.entity = entity
        self.unread_count = 0
        self.archived = False
        self.dialog = SimpleNamespace(
            unread_mark=False, notify_settings=SimpleNamespace(mute_until=None)
        )


class _DialogsClient:
    def __init__(self, dialogs):
        self._dialogs = dialogs

    async def get_dialogs(self, *args, **kwargs):
        return list(self._dialogs)


@pytest.mark.asyncio
async def test_list_chats_shows_only_readable_chats(monkeypatch):
    readable = SimpleNamespace(id=111, title="Readable", username=None)
    write_only = SimpleNamespace(id=222, title="Write only", username=None)
    client = _DialogsClient([_Dialog(readable), _Dialog(write_only)])

    async def _connected(cl):
        return None

    monkeypatch.setattr(chats, "get_client", lambda account=None: client)
    monkeypatch.setattr(chats, "ensure_connected", _connected)
    monkeypatch.setattr(chats, "get_marked_id", lambda e: -100000000000 - e.id)
    monkeypatch.setattr(chats, "get_entity_type", lambda e: "Channel")
    monkeypatch.setattr(chats, "get_entity_filter_type", lambda e: "channel")
    monkeypatch.setenv("TELEGRAM_ALLOWED_READ_CHATS", str(-100000000000 - 111))
    monkeypatch.setenv("TELEGRAM_ALLOWED_WRITE_CHATS", str(-100000000000 - 222))

    parsed = json.loads(await chats.list_chats(account=None)).get("results", [])
    assert [c["title"] for c in parsed] == ["Readable"]
