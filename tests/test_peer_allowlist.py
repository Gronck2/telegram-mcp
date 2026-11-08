"""Chat allowlists cover tools that list or name a peer outside validate_id (task 0002)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import functions, types

import main
from telegram_mcp import runtime
from telegram_mcp.tools import chats, contacts, groups, messages, profile

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
            "vanya the dev": {"id": 101, "name": "Ivan"},
            "secret": {"id": 303, "name": "Secret"},
        }
    )
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="101")

    data = json.loads(await contacts.list_contact_aliases())

    assert [row["id"] for row in data["results"]] == [101]
    assert sorted(data["results"][0]["aliases"]) == ["ivan", "vanya the dev"]
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


CHAT_KW = dict(participants_count=1, date=None, version=1, photo=types.ChatPhotoEmpty())


@pytest.mark.asyncio
async def test_get_privacy_settings_handles_channels_and_unknown_chats(monkeypatch):
    chan = types.Channel(id=77, title="C", photo=types.ChatPhotoEmpty(), date=None, access_hash=1)
    other = types.Channel(id=78, title="D", photo=types.ChatPhotoEmpty(), date=None, access_hash=1)
    settings = types.account.PrivacyRules(
        rules=[types.PrivacyValueAllowChatParticipants(chats=[77, 78, 79])],
        chats=[chan, other],
        users=[],
    )
    cl = AsyncMock(return_value=settings)
    monkeypatch.setattr(profile, "get_client", lambda account=None: cl)
    monkeypatch.setattr(profile, "ensure_connected", AsyncMock())
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="-1000000000077")

    data = json.loads(await profile.get_privacy_settings())

    assert data["rules"][0]["chats"] == [-1000000000077]


@pytest.mark.asyncio
async def test_get_privacy_settings_shows_public_view_for_outside_users(monkeypatch):
    other = types.User(id=303, first_name="Zed", phone="333", access_hash=3)
    settings = types.account.PrivacyRules(
        rules=[
            types.PrivacyValueAllowUsers(users=[101, 303]),
            types.PrivacyValueDisallowChatParticipants(chats=[5, 6]),
            types.PrivacyValueAllowContacts(),
        ],
        chats=[types.Chat(**CHAT_KW, id=5, title="A"), types.Chat(**CHAT_KW, id=6, title="B")],
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
    assert data["rules"][1]["chats"] == [-5]
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


# ---------------------------------------------------------------------------
# Group C: acting on a peer needs write access to the user's private chat
# ---------------------------------------------------------------------------


class ActionClient:
    """Records requests; answers every call with a harmless result."""

    def __init__(self):
        self.requests = []

    async def __call__(self, request, *args, **kwargs):
        self.requests.append(request)
        return SimpleNamespace(updates=[1], imported=[], users=[], rules=[], chats=[], peer=None)

    async def get_me(self):
        return SimpleNamespace(bot=False)


@pytest.fixture
def actions(monkeypatch):
    cl = ActionClient()

    async def resolve(identifier, c=None):
        if identifier in (101, "101", "ivan_k", "@ivan_k"):
            return IVAN
        raise ValueError("unknown")

    async def connected(c=None):
        return None

    for module in (contacts, profile, messages, runtime):
        monkeypatch.setattr(module, "get_client", lambda account=None: cl)
        monkeypatch.setattr(module, "resolve_entity", resolve)
    for module in (contacts, profile):
        monkeypatch.setattr(module, "ensure_connected", connected)
    return cl


USER_ACTIONS = [
    (contacts, "block_user", {"user_id": 101}),
    (contacts, "unblock_user", {"user_id": 101}),
    (contacts, "delete_contact", {"user_id": 101}),
    (profile, "set_privacy_settings", {"key": "phone", "allow_users": [101]}),
    (profile, "set_privacy_settings", {"key": "phone", "disallow_users": [101]}),
    (contacts, "add_contact", {"username": "ivan_k", "first_name": "Ivan"}),
    (
        profile,
        "set_bot_commands",
        {"bot_username": "ivan_k", "commands": [{"command": "a", "description": "b"}]},
    ),
    (contacts, "set_contact_alias", {"alias": "vanya the dev", "chat_id": "@ivan_k"}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("module, name, kwargs", USER_ACTIONS)
@pytest.mark.parametrize(
    "env",
    [
        {"TELEGRAM_ALLOWED_CHAT_IDS": "5"},
        {"TELEGRAM_ALLOWED_READ_CHATS": "*,101", "TELEGRAM_ALLOWED_WRITE_CHATS": "5"},
        {"TELEGRAM_ALLOWED_READ_CHATS": "101"},
    ],
)
async def test_acting_on_user_without_write_access_is_refused(
    monkeypatch, actions, module, name, kwargs, env
):
    allow(monkeypatch, **env)

    result = await getattr(module, name)(**kwargs)

    assert "Write access" in result and "restricted by privacy policy" in result
    assert actions.requests == []
    assert runtime.load_aliases() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("module, name, kwargs", USER_ACTIONS)
@pytest.mark.parametrize(
    "env",
    [
        {},
        {"TELEGRAM_ALLOWED_WRITE_CHATS": "101"},
        {"TELEGRAM_ALLOWED_WRITE_CHATS": "@ivan_k"},
        {"TELEGRAM_ALLOWED_CHAT_IDS": "*"},
    ],
)
async def test_acting_on_user_with_write_access_proceeds(
    monkeypatch, actions, module, name, kwargs, env
):
    allow(monkeypatch, **env)
    if "@ivan_k" in env.get("TELEGRAM_ALLOWED_WRITE_CHATS", ""):
        # the same user named by username instead of id
        kwargs = {
            k: (
                "@ivan_k" if v == 101 else ["@ivan_k"] if isinstance(v, list) and v == [101] else v
            )
            for k, v in kwargs.items()
        }

    result = await getattr(module, name)(**kwargs)

    assert "restricted by privacy policy" not in result


@pytest.mark.asyncio
async def test_acting_requests_reach_telegram_with_write_access(monkeypatch, actions):
    allow(monkeypatch, TELEGRAM_ALLOWED_WRITE_CHATS="101")

    assert await contacts.block_user(user_id=101) == "User 101 blocked."
    assert [type(r).__name__ for r in actions.requests] == ["BlockRequest"]


@pytest.mark.asyncio
async def test_set_contact_alias_checks_chat_id_before_saving(monkeypatch, actions):
    allow(monkeypatch, TELEGRAM_ALLOWED_WRITE_CHATS="101")

    saved = await contacts.set_contact_alias("vanya the dev", "101")
    assert '"saved": true' in saved.lower().replace('":true', '": true')

    denied = await contacts.set_contact_alias("other dev", "303")
    assert "restricted by privacy policy" in denied
    assert list(runtime.load_aliases()) == ["vanya the dev"]


@pytest.mark.asyncio
async def test_set_default_send_as_checks_send_as_but_not_me(monkeypatch, actions):
    allow(monkeypatch, TELEGRAM_ALLOWED_WRITE_CHATS="-1001,-1002")

    denied = await messages.set_default_send_as(chat_id=-1001, send_as=-1003)
    assert "Write access to chat '-1003' is restricted" in denied
    assert actions.requests == []

    # "me" is the own account: always allowed as a sender (chat checked separately)
    runtime_check = await runtime.deny_peer_param(
        "set_default_send_as", "send_as", "me", subject=True
    )
    assert runtime_check is None
    denied_chat = await messages.set_default_send_as(chat_id=-1009, send_as="me")
    assert "Write access to chat '-1009' is restricted" in denied_chat


@pytest.mark.asyncio
async def test_set_default_send_as_allowed_sender_proceeds(monkeypatch, actions):
    allow(monkeypatch, TELEGRAM_ALLOWED_WRITE_CHATS="-1001,-1002")

    async def resolve_input(identifier, c=None):
        return types.InputPeerChannel(1, 2) if identifier == -1001 else types.InputPeerSelf()

    monkeypatch.setattr(messages, "resolve_input_entity", resolve_input)

    result = await messages.set_default_send_as(chat_id=-1001, send_as="me")

    assert result == f"Default sender in chat -1001 set to {messages.SEND_AS_SELF_LABEL}."


@pytest.mark.asyncio
async def test_forward_message_send_as_needs_write_access(monkeypatch, actions):
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="-1001,-1002")

    denied = await messages.forward_message(
        from_chat_id=-1001, message_id=1, to_chat_id=-1002, send_as=-1003
    )

    assert "Write access to chat '-1003' is restricted" in denied
    assert actions.requests == []


@pytest.mark.asyncio
async def test_add_contact_by_phone_and_import_contacts_are_not_gated(monkeypatch, actions):
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="5")

    added = await contacts.add_contact(phone="+79990001122", first_name="Zed")
    imported = await contacts.import_contacts([{"phone": "+79990001122", "first_name": "Zed"}])

    assert "restricted by privacy policy" not in added + imported
    assert [type(r).__name__ for r in actions.requests] == [
        "ImportContactsRequest",
        "ImportContactsRequest",
    ]


@pytest.mark.asyncio
async def test_add_contact_failure_text_shows_public_view_only(monkeypatch, actions):
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="5")
    stranger = types.User(
        id=909,
        first_name="Zed",
        username="zed",
        phone="79991112233",
        verified=True,
        access_hash=3,
        status=types.UserStatusOnline(expires=None),
    )

    async def call(request, *args, **kwargs):
        return types.contacts.ImportedContacts(
            imported=[], popular_invites=[], retry_contacts=[], users=[stranger]
        )

    actions.__class__ = type("Failing", (ActionClient,), {"__call__": call})

    result = await contacts.add_contact(phone="+79991112233", first_name="Zed")

    assert result.startswith("Contact not added. Response:")
    assert "79991112233" not in result and "UserStatus" not in result
    assert '"username": "zed"' in result and "verified" in result


# ---------------------------------------------------------------------------
# Group D: user_id names a member inside a checked chat and must stay allowed
# ---------------------------------------------------------------------------

MEMBER_CALLS = [
    ("ban_user", {"chat_id": -1001, "user_id": 909}, "write"),
    ("unban_user", {"chat_id": -1001, "user_id": 909}, "write"),
    ("remove_user", {"chat_id": -1001, "user_id": 909}, "write"),
    ("promote_admin", {"group_id": -1001, "user_id": 909}, "write"),
    ("demote_admin", {"group_id": -1001, "user_id": 909}, "write"),
    ("edit_admin_rights", {"chat_id": -1001, "user_id": 909, "ban_users": True}, "write"),
    ("get_member_admin_status", {"chat_id": -1001, "user_id": 909}, "read"),
    ("invite_to_group", {"group_id": -1001, "user_ids": [909, "@stranger"]}, "write"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name, kwargs, mode", MEMBER_CALLS)
async def test_member_tools_accept_a_user_outside_the_allowlist(monkeypatch, name, kwargs, mode):
    """The chat is allowed, the member is not on any list: the call reaches the tool body."""
    env = "TELEGRAM_ALLOWED_READ_CHATS" if mode == "read" else "TELEGRAM_ALLOWED_WRITE_CHATS"
    allow(monkeypatch, **{env: "-1001"})
    reached = []

    def reaching_client(account=None):
        reached.append(name)
        raise RuntimeError("stop here")

    monkeypatch.setattr(groups, "get_client", reaching_client)
    monkeypatch.setattr(runtime, "get_client", reaching_client)

    result = await getattr(groups, name)(**kwargs)

    assert "restricted by privacy policy" not in result
    assert reached == [name]


@pytest.mark.asyncio
async def test_member_tools_still_refuse_a_chat_outside_the_allowlist(monkeypatch):
    allow(monkeypatch, TELEGRAM_ALLOWED_WRITE_CHATS="-1001")

    result = await groups.ban_user(chat_id=-1999, user_id=909)

    assert "Write access to chat '-1999' is restricted" in result


# ---------------------------------------------------------------------------
# Additional coverage: other peer types, alias candidates, no-allowlist parity
# ---------------------------------------------------------------------------

SECRET_BOT = types.User(
    id=202,
    first_name="Helper",
    username="helper_bot",
    bot=True,
    verified=True,
    phone="5550000",
    access_hash=2,
    photo=types.UserProfilePhoto(photo_id=777, dc_id=1),
)
SECRET_GROUP = types.Chat(
    id=5, title="Secret club", photo=None, participants_count=42, date=None, version=1
)
SECRET_CHANNEL = types.Channel(
    id=6,
    title="Hidden news",
    photo=None,
    date=None,
    broadcast=True,
    username="hidden_news",
    participants_count=999,
    verified=True,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entity, kind",
    [(SECRET_BOT, "bot"), (SECRET_GROUP, "group"), (SECRET_CHANNEL, "channel")],
)
async def test_get_full_user_public_view_for_bots_groups_and_channels(monkeypatch, entity, kind):
    cl = AsyncMock()

    async def resolve(identifier, c=None):
        return entity

    monkeypatch.setattr(profile, "get_client", lambda account=None: cl)
    monkeypatch.setattr(profile, "ensure_connected", AsyncMock())
    monkeypatch.setattr(profile, "resolve_entity", resolve)
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="1")

    text = await profile.get_full_user("x")
    data = json.loads(text)

    assert data["type"] == kind
    for forbidden in ("5550000", "777", "42", "999", "participants", "phone", "photo"):
        assert forbidden not in text.replace(str(data["id"]), "")
    cl.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_bot_info_public_view_for_a_real_bot(monkeypatch):
    cl = AsyncMock()

    async def resolve(identifier, c=None):
        return SECRET_BOT

    monkeypatch.setattr(profile, "get_client", lambda account=None: cl)
    monkeypatch.setattr(profile, "ensure_connected", AsyncMock())
    monkeypatch.setattr(profile, "resolve_entity", resolve)
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="1")

    text = await profile.get_bot_info("helper_bot")
    info = json.loads(text)["bot_info"]

    assert info["type"] == "bot" and info["bot"] is True and info["badges"] == ["verified"]
    for key in ("about", "description", "commands", "phone", "bot_commands"):
        assert key not in info
    assert "5550000" not in text
    cl.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_privacy_settings_unlisted_user_is_not_expanded(monkeypatch):
    """A user id missing from settings.users never gains extra fields."""
    settings = types.account.PrivacyRules(
        rules=[types.PrivacyValueDisallowUsers(users=[555])], chats=[], users=[]
    )
    cl = AsyncMock(return_value=settings)
    monkeypatch.setattr(profile, "get_client", lambda account=None: cl)
    monkeypatch.setattr(profile, "ensure_connected", AsyncMock())
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="1")

    data = json.loads(await profile.get_privacy_settings())

    assert data["rules"][0]["users"] == [{"id": 555}]


@pytest.mark.asyncio
async def test_set_contact_alias_ambiguous_name_does_not_leak_hidden_aliases(monkeypatch, actions):
    runtime.save_aliases({"vanya the dev": {"id": 303, "name": "Hidden Vanya"}})
    allow(monkeypatch, TELEGRAM_ALLOWED_WRITE_CHATS="101", TELEGRAM_ALLOWED_READ_CHATS="101")

    result = await contacts.set_contact_alias("new nick", "vanya the dev")

    assert "Hidden Vanya" not in result and "303" not in result
    assert "candidates" not in result
    assert actions.requests == []
    assert list(runtime.load_aliases()) == ["vanya the dev"]


@pytest.mark.asyncio
async def test_set_contact_alias_unchanged_without_allowlist(monkeypatch, actions):
    saved = await contacts.set_contact_alias("vanya the dev", "@ivan_k")

    assert '"saved": true' in saved.lower().replace('":true', '": true'), saved
    assert list(runtime.load_aliases()) == ["vanya the dev"]


@pytest.mark.asyncio
async def test_import_contacts_returns_only_a_count(monkeypatch, actions):
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="5")
    stranger = types.User(id=909, first_name="Zed", phone="79991112233", access_hash=3)

    async def call(request, *args, **kwargs):
        return types.contacts.ImportedContacts(
            imported=[types.ImportedContact(user_id=909, client_id=0)],
            popular_invites=[],
            retry_contacts=[],
            users=[stranger],
        )

    actions.__class__ = type("Importing", (ActionClient,), {"__call__": call})

    result = await contacts.import_contacts([{"phone": "+79991112233", "first_name": "Zed"}])

    assert result == "Imported 1 contacts."


@pytest.mark.asyncio
async def test_add_contact_failure_text_full_inside_read(monkeypatch, actions):
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="909")
    friend = types.User(id=909, first_name="Zed", phone="79991112233", access_hash=3)

    async def call(request, *args, **kwargs):
        return types.contacts.ImportedContacts(
            imported=[], popular_invites=[], retry_contacts=[], users=[friend]
        )

    actions.__class__ = type("Failing", (ActionClient,), {"__call__": call})

    result = await contacts.add_contact(phone="+79991112233", first_name="Zed")

    assert "79991112233" in result


@pytest.mark.asyncio
async def test_add_contact_failure_text_unchanged_without_allowlist(monkeypatch, actions):
    friend = types.User(id=909, first_name="Zed", phone="79991112233", access_hash=3)
    response = types.contacts.ImportedContacts(
        imported=[], popular_invites=[], retry_contacts=[], users=[friend]
    )

    async def call(request, *args, **kwargs):
        return response

    actions.__class__ = type("Failing", (ActionClient,), {"__call__": call})

    result = await contacts.add_contact(phone="+79991112233", first_name="Zed")

    assert result == f"Contact not added. Response: {response}"


@pytest.mark.asyncio
async def test_no_allowlist_group_c_tools_make_exactly_their_own_requests(monkeypatch, actions):
    await contacts.block_user(user_id=101)
    await contacts.unblock_user(user_id=101)
    await contacts.delete_contact(user_id=101)
    stranger = SimpleNamespace(users=[IVAN], updates=[1])
    original = actions.__class__.__call__

    async def call(self, request, *args, **kwargs):
        await original(self, request)
        if type(request).__name__ == "ResolveUsernameRequest":
            return stranger
        return SimpleNamespace(updates=[1])

    monkeypatch.setattr(actions.__class__, "__call__", call)
    await contacts.add_contact(username="ivan_k", first_name="Ivan")

    assert [type(r).__name__ for r in actions.requests] == [
        "BlockRequest",
        "UnblockRequest",
        "DeleteContactsRequest",
        "ResolveUsernameRequest",
        "AddContactRequest",
    ]


@pytest.mark.asyncio
async def test_no_allowlist_lookup_tools_are_unchanged(
    monkeypatch, profile_client, username_client
):
    full = json.loads(await profile.get_full_user("ivan_k"))
    bot = json.loads(await profile.get_bot_info("ivan_k"))
    status = await profile.get_user_status(user_id=101)

    assert full["phone"] == "79990001122" and full["bio"] == "SECRET-BIO"
    assert bot["bot_info"]["about"] == "SECRET-BIO"
    assert "UserStatusOnline" in status
    # one Telegram call per tool, nothing added by the allowlist code
    assert profile_client.await_count == 2

    assert json.loads(await chats.resolve_username("@ivan_k"))["phone"] == "79990001122"
    assert username_client.await_count == 1


@pytest.mark.asyncio
async def test_no_allowlist_contact_listings_unchanged(client):
    for name, kwargs in GROUP_A_CALLS:
        client.requests.clear()
        result = await getattr(contacts, name)(**kwargs)
        assert "restricted by privacy policy" not in result
        assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_alias_ask_payload_hides_aliases_outside_read_access(monkeypatch):
    runtime.save_aliases(
        {
            "boss visible": {"id": 101, "name": "Visible"},
            "boss hidden": {"id": 303, "name": "Hidden Boss"},
        }
    )
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="101")

    payload = json.loads(runtime.alias_ask_payload("boss"))
    text = json.dumps(payload)

    assert "Hidden Boss" not in text and "boss hidden" not in text and "303" not in text
    assert payload["known_aliases"] == ["boss visible"]
    assert [c["id"] for c in payload["candidates"]] == [101]


@pytest.mark.asyncio
async def test_alias_ask_payload_unchanged_without_allowlist():
    runtime.save_aliases({"boss hidden": {"id": 303, "name": "Hidden Boss"}})

    payload = json.loads(runtime.alias_ask_payload("boss"))

    assert payload["known_aliases"] == ["boss hidden"]


@pytest.mark.asyncio
async def test_set_contact_alias_already_used_hides_outside_read_record(monkeypatch, actions):
    runtime.save_aliases({"vanya the dev": {"id": 303, "name": "Hidden Boss", "account": "acc"}})
    allow(monkeypatch, TELEGRAM_ALLOWED_WRITE_CHATS="101", TELEGRAM_ALLOWED_READ_CHATS="101")

    text = await contacts.set_contact_alias("vanya the dev", "101")
    data = json.loads(text)["results"]

    assert data["reason"] == "alias_already_used"
    assert "current" not in data
    assert "Hidden Boss" not in text and "303" not in text


@pytest.mark.asyncio
async def test_set_contact_alias_already_used_shows_readable_record(monkeypatch, actions):
    runtime.save_aliases({"vanya the dev": {"id": 303, "name": "Zed"}})
    allow(monkeypatch, TELEGRAM_ALLOWED_WRITE_CHATS="101", TELEGRAM_ALLOWED_READ_CHATS="101,303")

    data = json.loads(await contacts.set_contact_alias("vanya the dev", "101"))["results"]

    assert data["current"]["id"] == 303


@pytest.mark.asyncio
async def test_set_contact_alias_resolved_is_public_view_outside_read(monkeypatch, actions):
    allow(monkeypatch, TELEGRAM_ALLOWED_WRITE_CHATS="101", TELEGRAM_ALLOWED_READ_CHATS="5")

    text = await contacts.set_contact_alias("vanya the dev", "101")

    assert json.loads(text)["results"]["saved"] is True
    assert "7999" not in text
    assert "79990001122" not in text


@pytest.mark.asyncio
async def test_own_account_is_full_view_for_lookup_tools(monkeypatch, profile_client):
    me = types.User(id=900, first_name="Me", phone="79991112233", is_self=True, access_hash=9)
    profile_client.return_value = SimpleNamespace(
        users=[me], full_user=full_user_response().full_user
    )

    async def resolve(identifier, c=None):
        return me

    monkeypatch.setattr(profile, "resolve_entity", resolve)
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="5")

    text = await profile.get_full_user("me")

    assert "SECRET-BIO" in text


# ---------------------------------------------------------------------------
# Round 1 fixes: missed paths and parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_contact_alias_fuzzy_target_hides_hidden_candidates(monkeypatch, actions):
    runtime.save_aliases({"vanya the dev": {"id": 303, "name": "Hidden Vanya"}})
    allow(monkeypatch, TELEGRAM_ALLOWED_WRITE_CHATS="101", TELEGRAM_ALLOWED_READ_CHATS="101")

    result = await contacts.set_contact_alias("new nick", "vanya")

    assert "Hidden Vanya" not in result and "303" not in result
    assert "vanya the dev" not in result
    assert list(runtime.load_aliases()) == ["vanya the dev"]


@pytest.mark.asyncio
async def test_hidden_alias_reference_in_a_tool_call_does_not_leak(monkeypatch, client):
    runtime.save_aliases({"vanya the dev": {"id": 303, "name": "Hidden Vanya"}})
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="101")

    text = await chats.get_chat("vanya the dev")

    assert "Hidden Vanya" not in text
    assert client.requests == []


@pytest.mark.asyncio
async def test_alias_ask_payload_stale_and_unknown_hide_hidden_records(monkeypatch):
    runtime.save_aliases({"boss hidden": {"id": 303, "name": "Hidden Boss"}})
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="101")

    for kind in ("unknown", "stale"):
        text = runtime.alias_ask_payload("boss", kind=kind, stored_id=101)
        assert "Hidden Boss" not in text and "boss hidden" not in text and "303" not in text


@pytest.mark.asyncio
async def test_alias_failure_flow_through_resolve_entity_hides_hidden_aliases(monkeypatch):
    runtime.save_aliases({"boss hidden": {"id": 303, "name": "Hidden Boss"}})
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="101")

    async def dead(*args, **kwargs):
        raise ValueError("nope")

    monkeypatch.setattr(runtime, "_resolve_with_retries", dead)

    with pytest.raises(runtime.AliasNeedsUser) as caught:
        await runtime.resolve_entity("boss", object())

    assert "Hidden Boss" not in caught.value.payload and "boss hidden" not in caught.value.payload


@pytest.mark.asyncio
async def test_alias_ask_payload_write_only_allowlist_hides_unreadable(monkeypatch):
    runtime.save_aliases({"boss hidden": {"id": 303, "name": "Hidden Boss"}})
    allow(monkeypatch, TELEGRAM_ALLOWED_WRITE_CHATS="303", TELEGRAM_ALLOWED_READ_CHATS="101")

    assert "boss hidden" not in runtime.alias_ask_payload("boss")


@pytest.mark.asyncio
async def test_set_contact_alias_same_target_repoint_is_not_refused(monkeypatch, actions):
    runtime.save_aliases({"vanya the dev": {"id": 101, "name": "Ivan"}})
    allow(monkeypatch, TELEGRAM_ALLOWED_WRITE_CHATS="101", TELEGRAM_ALLOWED_READ_CHATS="101")

    data = json.loads(await contacts.set_contact_alias("vanya the dev", "101"))["results"]

    assert data["saved"] is True


@pytest.mark.asyncio
async def test_set_contact_alias_resolved_full_inside_read_and_without_allowlist(
    monkeypatch, actions
):
    full = json.loads(await contacts.set_contact_alias("vanya the dev", "101"))["results"]
    assert full["resolved"]["phone"] == "7999"

    allow(monkeypatch, TELEGRAM_ALLOWED_WRITE_CHATS="101", TELEGRAM_ALLOWED_READ_CHATS="101")
    inside = json.loads(await contacts.set_contact_alias("vanya two dev", "101"))["results"]
    assert inside["resolved"]["phone"] == "7999"


@pytest.mark.asyncio
async def test_set_contact_alias_already_used_unchanged_without_allowlist(actions):
    runtime.save_aliases({"vanya the dev": {"id": 303, "name": "Zed"}})

    data = json.loads(await contacts.set_contact_alias("vanya the dev", "101"))["results"]

    assert data["reason"] == "alias_already_used"
    assert data["current"]["id"] == 303 and "Zed" in data["detail"]


def _privacy(monkeypatch, settings, **env):
    cl = AsyncMock(return_value=settings)
    monkeypatch.setattr(profile, "get_client", lambda account=None: cl)
    monkeypatch.setattr(profile, "ensure_connected", AsyncMock())
    allow(monkeypatch, **env)


@pytest.mark.asyncio
async def test_privacy_chats_basic_group_supergroup_channel_mix(monkeypatch):
    group = types.Chat(**CHAT_KW, id=5, title="G")
    sup = types.Channel(
        id=77, title="S", megagroup=True, photo=types.ChatPhotoEmpty(), date=None, access_hash=1
    )
    chan = types.Channel(id=78, title="C", photo=types.ChatPhotoEmpty(), date=None, access_hash=1)
    settings = types.account.PrivacyRules(
        rules=[types.PrivacyValueDisallowChatParticipants(chats=[5, 77, 78])],
        chats=[group, sup, chan],
        users=[],
    )
    _privacy(monkeypatch, settings, TELEGRAM_ALLOWED_READ_CHATS="-5,-1000000000078")

    data = json.loads(await profile.get_privacy_settings())

    assert data["rules"][0]["chats"] == [-5, -1000000000078]


@pytest.mark.asyncio
async def test_privacy_chats_none_allowed_gives_empty_list(monkeypatch):
    group = types.Chat(**CHAT_KW, id=5, title="G")
    settings = types.account.PrivacyRules(
        rules=[types.PrivacyValueAllowChatParticipants(chats=[5])], chats=[group], users=[]
    )
    _privacy(monkeypatch, settings, TELEGRAM_ALLOWED_READ_CHATS="101")

    data = json.loads(await profile.get_privacy_settings())

    assert data["rules"][0]["chats"] == []
    assert "G" not in json.dumps(data)


@pytest.mark.asyncio
async def test_privacy_users_own_account_is_full_view(monkeypatch):
    me = types.User(id=900, first_name="Me", phone="79991112233", is_self=True, access_hash=9)
    other = types.User(id=303, first_name="Zed", phone="333", access_hash=3)
    settings = types.account.PrivacyRules(
        rules=[
            types.PrivacyValueAllowUsers(users=[900, 303]),
            types.PrivacyValueDisallowUsers(users=[900]),
        ],
        chats=[],
        users=[me, other],
    )
    _privacy(monkeypatch, settings, TELEGRAM_ALLOWED_CHAT_IDS="1")

    data = json.loads(await profile.get_privacy_settings())

    assert data["rules"][0]["users"][0]["phone"] == "79991112233"
    assert data["rules"][0]["users"][1] == runtime.public_view(other)
    assert data["rules"][1]["users"][0]["id"] == 900
    assert "333" not in json.dumps(data)


@pytest.mark.asyncio
async def test_privacy_chats_unchanged_without_allowlist(monkeypatch):
    settings = types.account.PrivacyRules(
        rules=[types.PrivacyValueAllowChatParticipants(chats=[5, 6])], chats=[], users=[]
    )
    _privacy(monkeypatch, settings)

    assert await profile.get_privacy_settings() == str(settings)


def test_peer_allowed_own_account_only_when_is_self_true(monkeypatch):
    me = types.User(id=900, first_name="Me", is_self=True, access_hash=9)
    notme = types.User(id=901, first_name="No", is_self=False, access_hash=9)
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="5")

    for access in (runtime.CHAT_ACCESS_READ, runtime.CHAT_ACCESS_WRITE):
        assert runtime.peer_allowed(me, access) is True
        assert runtime.peer_allowed(notme, access) is False
    # A truthy non-bool must not open the gate.
    assert runtime.peer_allowed(SimpleNamespace(id=901, is_self="yes"), "read") is False


def test_peer_allowed_without_allowlist_is_true():
    assert runtime.peer_allowed(types.User(id=901, first_name="No", access_hash=1)) is True


@pytest.mark.asyncio
async def test_me_in_group_b_tools_under_allowlist(monkeypatch, profile_client):
    me = types.User(id=900, first_name="Me", phone="79991112233", is_self=True, access_hash=9)
    profile_client.return_value = SimpleNamespace(
        users=[me], full_user=full_user_response().full_user
    )

    async def resolve(identifier, c=None):
        return me

    monkeypatch.setattr(profile, "resolve_entity", resolve)
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="5")

    for name in ("get_user_photos", "get_user_status"):
        text = await getattr(profile, name)("me")
        assert "restricted by privacy policy" not in text


# ---------------------------------------------------------------------------
# Code-review round 2: "@self", hidden aliases, set_contact_alias candidates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_at_self_is_checked_as_a_username_but_bare_me_self_pass(monkeypatch):
    other = types.User(id=777, first_name="Not Me", username="self", access_hash=1)
    seen = []

    async def resolve(identifier, c=None):
        seen.append(identifier)
        return other

    monkeypatch.setattr(runtime, "get_client", lambda account=None: object())
    monkeypatch.setattr(runtime, "resolve_entity", resolve)
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="101")

    for ref in ("@self", "@me", "@Self", "Me", "SELF"):
        denied = await runtime.deny_peer_param("block_user", "user_id", ref, subject=True)
        assert denied is not None and "restricted by privacy" in denied, ref
    for ref in ("me", "self"):
        assert await runtime.deny_peer_param("block_user", "user_id", ref, subject=True) is None
    assert seen == ["@self", "@me", "@Self", "Me", "SELF"]


@pytest.mark.asyncio
async def test_at_self_resolving_to_the_own_account_is_allowed(monkeypatch):
    own = SimpleNamespace(id=5, is_self=True)

    async def resolve(identifier, c=None):
        return own

    monkeypatch.setattr(runtime, "get_client", lambda account=None: object())
    monkeypatch.setattr(runtime, "resolve_entity", resolve)
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="101")

    assert await runtime.deny_peer_param("block_user", "user_id", "@x_own", subject=True) is None


@pytest.mark.asyncio
async def test_hidden_alias_is_not_resolved_by_lookup_tools(monkeypatch, actions):
    runtime.save_aliases({"лена": {"id": 303, "name": "Hidden Lena"}})
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="101")

    assert runtime.apply_alias("лена") == "лена"
    for name in ("get_full_user", "get_bot_info"):
        text = await getattr(profile, name)("лена")
        assert "Hidden Lena" not in text and "303" not in text, name
    assert actions.requests == []


@pytest.mark.asyncio
async def test_write_only_alias_still_resolves(monkeypatch):
    runtime.save_aliases({"лена": {"id": 303, "name": "Lena"}})
    allow(monkeypatch, TELEGRAM_ALLOWED_WRITE_CHATS="303", TELEGRAM_ALLOWED_READ_CHATS="101")

    assert runtime.apply_alias("лена") == 303


def test_alias_unchanged_without_allowlist():
    runtime.save_aliases({"лена": {"id": 303, "name": "Lena"}})

    assert runtime.apply_alias("лена") == 303


@pytest.mark.asyncio
async def test_denial_text_names_the_wording_not_the_stored_id(monkeypatch):
    runtime.save_aliases({"лена": {"id": 303, "name": "Lena"}})
    allow(monkeypatch, TELEGRAM_ALLOWED_WRITE_CHATS="303", TELEGRAM_ALLOWED_READ_CHATS="101")

    denied = await runtime.deny_peer_param(
        "get_full_user", "user_id", runtime.AliasID(303, "лена"), subject=True
    )

    assert denied is not None and "лена" in denied and "303" not in denied


@pytest.mark.asyncio
async def test_stale_hidden_alias_gives_no_stored_id(monkeypatch):
    runtime.save_aliases({"лена": {"id": 303, "name": "Hidden Lena"}})
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="101")

    async def dead(*args, **kwargs):
        raise ValueError("nope")

    monkeypatch.setattr(runtime, "_resolve_with_retries", dead)

    with pytest.raises(runtime.AliasNeedsUser) as caught:
        await runtime.resolve_entity("лена", object())

    payload = caught.value.payload
    assert "303" not in payload and "Hidden Lena" not in payload and "stale" not in payload


@pytest.mark.asyncio
async def test_set_contact_alias_ambiguous_candidates_filtered_with_write_star(
    monkeypatch, actions
):
    runtime.save_aliases(
        {
            "vanya the hidden": {"id": 303, "name": "Hidden Vanya"},
            "vanya the open": {"id": 101, "name": "Open Vanya"},
        }
    )
    allow(monkeypatch, TELEGRAM_ALLOWED_WRITE_CHATS="*", TELEGRAM_ALLOWED_READ_CHATS="101")

    result = await contacts.set_contact_alias("new nick", "vanya the")

    assert "Hidden Vanya" not in result and "303" not in result and "the hidden" not in result
    assert "Open Vanya" in result


# ---------------------------------------------------------------------------
# Round 2 follow-up: validate_id path, visible_alias_records, read-only aliases
# ---------------------------------------------------------------------------


def _validated(access_name):
    """A tool body behind validate_id, named so its read/write mode is known."""
    calls = []

    async def body(chat_id, account=None):
        calls.append(chat_id)
        return "ran"

    body.__name__ = access_name
    body = runtime.validate_id("chat_id")(body)

    return body, calls


@pytest.mark.asyncio
async def test_validate_id_hidden_alias_neither_resolves_nor_leaks(monkeypatch):
    runtime.save_aliases({"лена": {"id": 303, "name": "Hidden Lena"}})
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="101", TELEGRAM_ALLOWED_WRITE_CHATS="101")
    body, calls = _validated("send_message")

    result = await body(chat_id="лена")

    assert calls == []
    assert "303" not in result and "Hidden Lena" not in result


@pytest.mark.asyncio
async def test_validate_id_read_only_alias_denied_for_write_without_stored_id(monkeypatch):
    runtime.save_aliases({"лена": {"id": 303, "name": "Lena"}})
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="303", TELEGRAM_ALLOWED_WRITE_CHATS="101")
    body, calls = _validated("send_message")

    result = await body(chat_id="лена")

    assert calls == []
    assert "лена" in result and "303" not in result


@pytest.mark.asyncio
async def test_validate_id_read_only_alias_passes_for_read_tool(monkeypatch):
    runtime.save_aliases({"лена": {"id": 303, "name": "Lena"}})
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="303", TELEGRAM_ALLOWED_WRITE_CHATS="101")
    body, calls = _validated("get_messages")

    assert await body(chat_id="лена") == "ran"
    assert calls == [303]


@pytest.mark.asyncio
async def test_validate_id_write_only_alias_resolves_for_write_tool(monkeypatch):
    runtime.save_aliases({"лена": {"id": 303, "name": "Lena"}})
    allow(monkeypatch, TELEGRAM_ALLOWED_WRITE_CHATS="303", TELEGRAM_ALLOWED_READ_CHATS="101")
    body, calls = _validated("send_message")

    assert await body(chat_id="лена") == "ran"
    assert calls == [303]


@pytest.mark.asyncio
async def test_validate_id_at_self_is_not_a_self_reference(monkeypatch):
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="101")
    seen = []

    async def resolve(identifier, c=None):
        seen.append(identifier)
        return SimpleNamespace(id=999, is_self=False, username="self")

    monkeypatch.setattr(runtime, "resolve_entity", resolve)
    monkeypatch.setattr(runtime, "get_client", lambda account=None: object())
    body, calls = _validated("get_messages")

    result = await body(chat_id="@self")

    assert seen == ["@self"]
    assert calls == [] and "restricted" in result


RECORDS = {
    "open": {"id": 101, "name": "Open"},
    "hidden": {"id": 303, "name": "Hidden"},
}


def test_visible_alias_records_passthrough_without_allowlist():
    pairs = list(RECORDS.items())

    assert runtime.visible_alias_records(pairs) is pairs
    assert runtime.visible_alias_records(RECORDS) is RECORDS


def test_visible_alias_records_filters_list_and_dict_by_read(monkeypatch):
    allow(monkeypatch, TELEGRAM_ALLOWED_READ_CHATS="101", TELEGRAM_ALLOWED_WRITE_CHATS="303")

    assert runtime.visible_alias_records(list(RECORDS.items())) == [("open", RECORDS["open"])]
    assert runtime.visible_alias_records(RECORDS) == {"open": RECORDS["open"]}
    assert runtime.visible_alias_records([]) == []
    assert runtime.visible_alias_records({}) == {}


@pytest.mark.asyncio
async def test_no_allowlist_alias_flow_makes_no_extra_telegram_calls(actions):
    runtime.save_aliases({"лена": {"id": 303, "name": "Lena"}})

    assert runtime.apply_alias("лена") == 303
    assert (
        await runtime.deny_peer_param("get_full_user", "user_id", "@whoever", subject=True) is None
    )
    assert await runtime.deny_peer_param("block_user", "user_id", ["me", "лена"]) is None
    assert actions.requests == []


@pytest.mark.asyncio
async def test_chat_parameter_naming_the_own_account_needs_the_own_id_listed(monkeypatch):
    own = SimpleNamespace(id=5, is_self=True)

    async def resolve(identifier, c=None):
        return own

    monkeypatch.setattr(runtime, "get_client", lambda account=None: object())
    monkeypatch.setattr(runtime, "resolve_entity", resolve)
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="101")

    for ref in ("me", "@own_name"):
        # a chat parameter is Saved Messages, not a subject: refused unless 5 is listed
        denied = await runtime.deny_peer_param("list_messages", "chat_id", ref)
        assert denied is not None and "restricted by privacy" in denied, ref
        res = await messages.list_messages(chat_id=ref)
        assert "restricted by privacy" in res, ref
        # as a subject parameter the own account stays allowed
        assert (
            await runtime.deny_peer_param("block_user", "user_id", ref, subject=True) is None
        ), ref

    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="101,5")
    assert await runtime.deny_peer_param("list_messages", "chat_id", "@own_name") is None


@pytest.mark.asyncio
async def test_add_contact_alias_does_not_launder_a_hidden_username(monkeypatch, actions):
    boss = types.User(id=303, first_name="Boss", username="boss", access_hash=3)

    async def call(self, request, *args, **kwargs):
        actions.requests.append(request)
        if type(request).__name__ == "ResolveUsernameRequest":
            return SimpleNamespace(users=[boss])
        return SimpleNamespace(updates=[1])

    async def resolve(identifier, c=None):
        if identifier == "boss":  # local alias -> the allowed user
            return IVAN
        raise ValueError("unknown")

    monkeypatch.setattr(actions.__class__, "__call__", call)
    for module in (contacts, runtime):
        monkeypatch.setattr(module, "resolve_entity", resolve)
    allow(monkeypatch, TELEGRAM_ALLOWED_CHAT_IDS="101")

    result = await contacts.add_contact(username="boss", first_name="Boss")

    assert "restricted by privacy" in result
    assert "AddContactRequest" not in [type(r).__name__ for r in actions.requests]
