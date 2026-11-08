"""Chat allowlists cover tools that list or name a peer outside validate_id (task 0002)."""

import json

import pytest
from telethon import functions, types

import main
from telegram_mcp import runtime
from telegram_mcp.tools import contacts

ENV_VARS = (
    "TELEGRAM_ALLOWED_CHAT_IDS",
    "TELEGRAM_ALLOWED_READ_CHATS",
    "TELEGRAM_ALLOWED_WRITE_CHATS",
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", None)
    monkeypatch.setattr(main, "ALLOWED_CHAT_IDS", None)
    monkeypatch.setenv("TELEGRAM_ALIASES_FILE", str(tmp_path / "aliases.json"))


def allow(monkeypatch, **env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)


# ---------------------------------------------------------------------------
# Inventory: every registered tool with a peer-naming parameter is classified
# ---------------------------------------------------------------------------

# Parameters that name a user or peer outside the chat parameters validate_id
# already checks. Chat parameters (runtime.CHAT_PARAM_NAMES) are covered by
# validate_id or an in-body check.
PEER_PARAMS = frozenset(
    {
        "user_id",
        "user_ids",
        "username",
        "bot_username",
        "phone",
        "allow_users",
        "disallow_users",
        "send_as",
        "contacts",
        "contact_query",
    }
)

# Group B: looks up one named peer; outside read access only the public view.
LOOKUP_TOOLS = {
    "resolve_username",
    "get_full_user",
    "get_bot_info",
    "resolve_phone",
    "get_user_photos",
    "get_user_status",
}
# Group C: acts on a named peer; write access to the user's private chat.
ACTING_TOOLS = {
    "block_user",
    "unblock_user",
    "delete_contact",
    "set_privacy_settings",
    "set_bot_commands",
    "set_default_send_as",
    "forward_message",
    "add_contact",
}
# add_contact by phone / import_contacts: allowed by owner decision 4.
UNGATED_BY_DECISION = {"import_contacts"}
# Group D: user_id names a member acted on inside an already-checked chat.
MEMBER_TOOLS = {
    "ban_user",
    "unban_user",
    "remove_user",
    "promote_admin",
    "demote_admin",
    "edit_admin_rights",
    "get_member_admin_status",
    "invite_to_group",
}
# Other exempt tools: results are filtered by the allowlist, or the value is
# content sent to a chat, or the out-of-scope group/channel creators.
EXEMPT_TOOLS = {
    "get_common_chats",
    "get_direct_chat_by_contact",
    "send_contact",
    "create_group",
    "create_folder",  # `contacts` is a boolean folder filter, not a peer
}


def _registered_tools():
    import telegram_mcp.server  # noqa: F401  (registers every tool)

    return runtime.mcp._tool_manager.list_tools()


def test_every_peer_naming_tool_is_classified():
    classified = LOOKUP_TOOLS | ACTING_TOOLS | UNGATED_BY_DECISION | MEMBER_TOOLS | EXEMPT_TOOLS
    unclassified = sorted(
        tool.name
        for tool in _registered_tools()
        if PEER_PARAMS & set(tool.parameters.get("properties", {})) and tool.name not in classified
    )
    assert unclassified == [], (
        f"Tools with a peer-naming parameter must be classified in tests/test_peer_allowlist.py "
        f"and handled by the allowlist: {unclassified}"
    )


def test_classified_tools_exist():
    names = {tool.name for tool in _registered_tools()}
    classified = LOOKUP_TOOLS | ACTING_TOOLS | UNGATED_BY_DECISION | MEMBER_TOOLS | EXEMPT_TOOLS
    assert classified <= names


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

IVAN = types.User(id=101, first_name="Ivan", username="ivan_k", access_hash=1, phone="7999")
BOT = types.User(id=202, first_name="Helper", username="helper_bot", bot=True, access_hash=2)


def test_read_access_restricted_cases(monkeypatch):
    assert runtime.read_access_restricted() is False
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="1")
    assert runtime.read_access_restricted() is True
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="*")
    assert runtime.read_access_restricted() is False
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "*")
    monkeypatch.delenv("TELEGRAM_ALLOWED_READ_CHATS")
    assert runtime.read_access_restricted() is False
    # a write-only wildcard does not open reading
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    monkeypatch.setenv("TELEGRAM_ALLOWED_WRITE_CHATS", "*")
    assert runtime.read_access_restricted() is True


def test_public_view_uses_only_the_allowed_fields():
    me = types.User(
        id=7,
        first_name="Ann",
        last_name="Lee",
        username="ann",
        phone="1234",
        verified=True,
        premium=True,
        scam=False,
        access_hash=9,
        status=types.UserStatusOnline(expires=None),
        usernames=[types.Username(username="ann2", editable=False, active=True)],
        photo=types.UserProfilePhoto(photo_id=5, dc_id=1),
    )
    view = runtime.public_view(me)
    assert view == {
        "id": 7,
        "type": "user",
        "bot": False,
        "first_name": "Ann",
        "last_name": "Lee",
        "username": "ann",
        "additional_usernames": ["ann2"],
        "badges": ["verified", "premium"],
    }


def test_public_view_types():
    assert runtime.public_view(BOT)["type"] == "bot"
    group = types.Chat(id=5, title="G", photo=None, participants_count=1, date=None, version=1)
    assert runtime.public_view(group)["type"] == "group"
    assert runtime.public_view(group)["id"] == -5
    mega = types.Channel(id=6, title="M", photo=None, date=None, megagroup=True)
    assert runtime.public_view(mega)["type"] == "group"
    chan = types.Channel(id=6, title="C", photo=None, date=None, broadcast=True)
    view = runtime.public_view(chan)
    assert view == {"id": -1000000000006, "type": "channel", "title": "C", "badges": []}


# ---------------------------------------------------------------------------
# Group A: contact-book listings are refused while read access is restricted
# ---------------------------------------------------------------------------


class RecordingClient:
    def __init__(self):
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, functions.contacts.GetContactIDsRequest):
            return [101]
        if isinstance(request, functions.contacts.GetBlockedRequest):
            return types.contacts.Blocked(blocked=[], chats=[], users=[IVAN])
        return types.contacts.Contacts(contacts=[], saved_count=0, users=[IVAN])


@pytest.fixture
def client(monkeypatch):
    cl = RecordingClient()

    async def connected(c=None):
        return None

    monkeypatch.setattr(contacts, "get_client", lambda account=None: cl)
    monkeypatch.setattr(contacts, "ensure_connected", connected)
    return cl


GROUP_A_CALLS = [
    ("list_contacts", {}),
    ("search_contacts", {"query": "iv"}),
    ("export_contacts", {}),
    ("get_contact_ids", {}),
    ("get_blocked_users", {}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name, kwargs", GROUP_A_CALLS)
@pytest.mark.parametrize(
    "env",
    [
        {"TELEGRAM_ALLOWED_CHAT_IDS": "5"},
        {"TELEGRAM_ALLOWED_READ_CHATS": "5"},
        {"TELEGRAM_ALLOWED_READ_CHATS": "5", "TELEGRAM_ALLOWED_WRITE_CHATS": "*"},
    ],
)
async def test_group_a_refused_when_read_restricted(monkeypatch, client, name, kwargs, env):
    allow(monkeypatch, **env)

    result = await getattr(contacts, name)(**kwargs)

    assert "restricted by privacy policy" in result
    assert "Ivan" not in result and "7999" not in result
    assert client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name, kwargs", GROUP_A_CALLS)
@pytest.mark.parametrize(
    "env",
    [
        {},
        {"TELEGRAM_ALLOWED_READ_CHATS": "*"},
        {"TELEGRAM_ALLOWED_READ_CHATS": "*", "TELEGRAM_ALLOWED_WRITE_CHATS": "5"},
        {"TELEGRAM_ALLOWED_CHAT_IDS": "*"},
    ],
)
async def test_group_a_works_without_restriction(monkeypatch, client, name, kwargs, env):
    allow(monkeypatch, **env)

    result = await getattr(contacts, name)(**kwargs)

    assert "restricted by privacy policy" not in result
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_list_contact_aliases_filters_by_read_access(monkeypatch):
    runtime.save_aliases(
        {
            "ivan": {"id": 101, "name": "Ivan"},
            "vanya": {"id": 101, "name": "Ivan"},
            "secret": {"id": 303, "name": "Secret"},
        }
    )
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="101")

    data = json.loads(await contacts.list_contact_aliases())

    assert [row["id"] for row in data["results"]] == [101]
    assert sorted(data["results"][0]["aliases"]) == ["ivan", "vanya"]
    assert "Secret" not in json.dumps(data) and "303" not in json.dumps(data)
    assert "hidden" not in json.dumps(data).lower()


@pytest.mark.asyncio
async def test_list_contact_aliases_all_hidden_looks_empty(monkeypatch):
    runtime.save_aliases({"secret": {"id": 303, "name": "Secret"}})
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="101")

    assert await contacts.list_contact_aliases() == "No aliases saved."


@pytest.mark.asyncio
async def test_list_contact_aliases_unchanged_without_allowlist():
    runtime.save_aliases({"secret": {"id": 303, "name": "Secret"}})

    data = json.loads(await contacts.list_contact_aliases())

    assert data["results"][0]["id"] == 303
