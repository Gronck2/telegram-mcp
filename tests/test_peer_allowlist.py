"""Chat allowlists cover tools that list or name a peer outside validate_id (task 0002)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import functions, types

import main
from telegram_mcp import runtime
from telegram_mcp.tools import chats, contacts, profile

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


# ---------------------------------------------------------------------------
# Group B: lookups on one named peer return the public view outside read access
# ---------------------------------------------------------------------------

RICH = types.User(
    id=101,
    first_name="Ivan",
    last_name="Petrov",
    username="ivan_k",
    phone="79990001122",
    verified=True,
    contact=True,
    mutual_contact=True,
    access_hash=1,
    status=types.UserStatusOnline(expires=None),
    photo=types.UserProfilePhoto(photo_id=555, dc_id=1),
)
HIDDEN_VALUES = ("79990001122", "SECRET-BIO", "555", "UserStatus", "1990", "personal_chan")


def assert_public_only(text):
    for value in HIDDEN_VALUES:
        assert value not in text, value
    for field in (
        "phone",
        "bio",
        "about",
        "birthday",
        "personal_channel",
        "common_chats_count",
        "contact",
        "mutual_contact",
        "status",
        "photo",
    ):
        assert f'"{field}"' not in text, field


def full_user_response():
    full = SimpleNamespace(
        about="SECRET-BIO",
        personal_channel_id=None,
        birthday=SimpleNamespace(day=1, month=2, year=1990),
        common_chats_count=7,
        private_forward_name=None,
        pinned_msg_id=None,
        stargifts_count=None,
    )
    return SimpleNamespace(users=[RICH], full_user=full)


@pytest.fixture
def profile_client(monkeypatch):
    cl = AsyncMock(return_value=full_user_response())

    async def resolve(identifier, c=None):
        return RICH

    monkeypatch.setattr(profile, "get_client", lambda account=None: cl)
    monkeypatch.setattr(profile, "ensure_connected", AsyncMock())
    monkeypatch.setattr(profile, "resolve_entity", resolve)
    monkeypatch.setattr(runtime, "get_client", lambda account=None: cl)
    monkeypatch.setattr(runtime, "resolve_entity", resolve)
    return cl


@pytest.mark.asyncio
async def test_get_full_user_outside_read_is_public_view(monkeypatch, profile_client):
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="5")

    text = await profile.get_full_user("ivan_k")

    assert_public_only(text)
    assert json.loads(text) == {
        "id": 101,
        "type": "user",
        "bot": False,
        "first_name": "Ivan",
        "last_name": "Petrov",
        "username": "ivan_k",
        "badges": ["verified"],
    }
    profile_client.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env",
    [{}, {"TELEGRAM_ALLOWED_CHAT_IDS": "101"}, {"TELEGRAM_ALLOWED_READ_CHATS": "*"}],
)
async def test_get_full_user_inside_read_or_no_allowlist_is_full(monkeypatch, profile_client, env):
    allow(monkeypatch, **env)

    data = json.loads(await profile.get_full_user("ivan_k"))

    assert data["bio"] == "SECRET-BIO"
    assert data["phone"] == "79990001122"
    assert data["common_chats_count"] == 7


@pytest.mark.asyncio
async def test_get_bot_info_outside_read_omits_description(monkeypatch, profile_client):
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="5")

    text = await profile.get_bot_info("ivan_k")

    assert_public_only(text)
    assert json.loads(text) == {"bot_info": runtime.public_view(RICH)}
    profile_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_bot_info_inside_read_is_unchanged(monkeypatch, profile_client):
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="101")

    data = json.loads(await profile.get_bot_info("ivan_k"))

    assert data["bot_info"]["about"] == "SECRET-BIO"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["get_user_photos", "get_user_status"])
async def test_photos_and_status_refused_outside_read(monkeypatch, profile_client, name):
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="5")

    for user_id in (101, "ivan_k"):
        result = await getattr(profile, name)(user_id=user_id)
        assert "restricted by privacy policy" in result
    profile_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_photos_and_status_work_inside_read(monkeypatch, profile_client):
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="101")
    profile_client.return_value = SimpleNamespace(photos=[SimpleNamespace(id=555)])

    assert json.loads(await profile.get_user_photos(user_id=101)) == [555]
    assert "UserStatusOnline" in await profile.get_user_status(user_id=101)


@pytest.mark.asyncio
async def test_username_input_is_checked_after_resolving(monkeypatch, profile_client):
    """A handle allowlisted by username is allowed once it resolves to that user."""
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="@ivan_k")
    profile_client.return_value = SimpleNamespace(photos=[SimpleNamespace(id=555)])

    assert json.loads(await profile.get_user_photos(user_id="@ivan_k")) == [555]
    # a bare numeric id is compared as a marked id only, so it fails closed
    assert "restricted by privacy policy" in await profile.get_user_status(user_id=101)


def resolved_username_response(user):
    return SimpleNamespace(peer=types.PeerUser(user_id=user.id), users=[user], chats=[])


@pytest.fixture
def username_client(monkeypatch):
    cl = AsyncMock(return_value=resolved_username_response(RICH))
    monkeypatch.setattr(chats, "get_client", lambda account=None: cl)
    monkeypatch.setattr(chats, "ensure_connected", AsyncMock())
    return cl


@pytest.mark.asyncio
async def test_resolve_username_outside_read_is_public_view(monkeypatch, username_client):
    allow(monkeypatch, TELEGRAM_ALLOWED_WRITE_CHATS="101")

    text = await chats.resolve_username("@ivan_k")
    data = json.loads(text)

    assert_public_only(text)
    assert data["id"] == 101 and data["first_name"] == "Ivan" and data["badges"] == ["verified"]
    assert data["peer_id"] == 101


@pytest.mark.asyncio
async def test_resolve_username_channel_outside_read_hides_counts(monkeypatch, username_client):
    channel = types.Channel(
        id=9,
        title="News",
        photo=None,
        date=None,
        broadcast=True,
        username="news",
        participants_count=12345,
    )
    username_client.return_value = SimpleNamespace(
        peer=types.PeerChannel(channel_id=9), users=[], chats=[channel]
    )
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="5")

    data = json.loads(await chats.resolve_username("news"))

    assert data["type"] == "channel" and data["title"] == "News"
    assert "participants_count" not in data and "broadcast" not in data


@pytest.mark.asyncio
async def test_resolve_username_inside_read_is_full(monkeypatch, username_client):
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="101")

    data = json.loads(await chats.resolve_username("@ivan_k"))

    assert data["phone"] == "79990001122"


@pytest.mark.asyncio
async def test_resolve_phone_outside_read_is_public_view(monkeypatch):
    result = types.contacts.ResolvedPeer(peer=types.PeerUser(user_id=101), chats=[], users=[RICH])
    cl = AsyncMock(return_value=result)
    monkeypatch.setattr(contacts, "get_client", lambda account=None: cl)
    monkeypatch.setattr(contacts, "ensure_connected", AsyncMock())
    monkeypatch.setattr(contacts, "_resolve_phone_limiter", contacts.ResolvePhoneLimiter())
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="5")

    text = await contacts.resolve_phone("+7 999 000 1122")

    assert_public_only(text)
    assert json.loads(text)["id"] == 101
    cl.assert_awaited_once()

    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="101")
    assert "79990001122" in await contacts.resolve_phone("+7 999 000 1122")


@pytest.mark.asyncio
async def test_resolve_phone_without_user_under_allowlist_does_not_dump_result(monkeypatch):
    result = types.contacts.ResolvedPeer(peer=types.PeerUser(user_id=101), chats=[], users=[])
    cl = AsyncMock(return_value=result)
    monkeypatch.setattr(contacts, "get_client", lambda account=None: cl)
    monkeypatch.setattr(contacts, "ensure_connected", AsyncMock())
    monkeypatch.setattr(contacts, "_resolve_phone_limiter", contacts.ResolvePhoneLimiter())
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="5")

    assert await contacts.resolve_phone("+7 999 000 1122") == contacts.NO_PHONE_USER_MESSAGE


@pytest.mark.asyncio
async def test_get_privacy_settings_shows_public_view_for_outside_users(monkeypatch):
    other = types.User(id=303, first_name="Zed", phone="333", access_hash=3)
    settings = types.account.PrivacyRules(
        rules=[
            types.PrivacyValueAllowUsers(users=[101, 303]),
            types.PrivacyValueDisallowChatParticipants(chats=[5, 6]),
            types.PrivacyValueAllowContacts(),
        ],
        chats=[],
        users=[RICH, other],
    )
    cl = AsyncMock(return_value=settings)
    monkeypatch.setattr(profile, "get_client", lambda account=None: cl)
    monkeypatch.setattr(profile, "ensure_connected", AsyncMock())
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="101,-5")

    text = await profile.get_privacy_settings()
    data = json.loads(text)

    allow_users = data["rules"][0]["users"]
    assert allow_users[0]["phone"] == "79990001122"  # inside read: full
    assert allow_users[1] == runtime.public_view(other)  # outside read: public view
    assert "333" not in text
    assert data["rules"][1]["chats"] == [5]
    assert data["rules"][2] == {"rule": "PrivacyValueAllowContacts"}


@pytest.mark.asyncio
async def test_get_privacy_settings_unchanged_without_allowlist(monkeypatch):
    settings = types.account.PrivacyRules(
        rules=[types.PrivacyValueAllowUsers(users=[101])], chats=[], users=[RICH]
    )
    cl = AsyncMock(return_value=settings)
    monkeypatch.setattr(profile, "get_client", lambda account=None: cl)
    monkeypatch.setattr(profile, "ensure_connected", AsyncMock())

    assert await profile.get_privacy_settings() == str(settings)
