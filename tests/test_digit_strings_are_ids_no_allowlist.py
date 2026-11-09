"""Digit strings are ids with or without an allowlist (task 0006, round 3).

Left as strings, Telethon reads them as phones: a miss then reaches the display-name
column (a chat titled "-1001234567890") or GetContacts (a user whose phone matches).
"""

from types import SimpleNamespace

import pytest
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession

from telegram_mcp import runtime
from telegram_mcp.tools import folders, messages, profile

IMPOSTOR_ID = 1001
PHONE_USER = types.User(id=15551234567, first_name="PhoneId", access_hash=11)
PHONE_OWNER = types.User(id=555, first_name="Owner", phone="15551234567", access_hash=3)
SMALL = types.User(id=101, first_name="Small", access_hash=6)
CHAN = types.Channel(
    id=1234567890,
    title="Real",
    photo=types.ChatPhotoEmpty(),
    date=None,
    access_hash=9,
    megagroup=True,
)
# spelling -> (expected peer id field, expected value)
CASES = {
    "-1001234567890": ("channel_id", 1234567890),
    "15551234567": ("user_id", 15551234567),
    " 101 ": ("user_id", 101),
}
GetContacts = functions.contacts.GetContactsRequest


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for name in (
        "TELEGRAM_ALLOWED_CHAT_IDS",
        "TELEGRAM_ALLOWED_READ_CHATS",
        "TELEGRAM_ALLOWED_WRITE_CHATS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", None)
    monkeypatch.setenv("TELEGRAM_ALIASES_FILE", str(tmp_path / "aliases.json"))

    async def connected(client=None):
        return None

    for module in (runtime, messages, profile, folders):
        monkeypatch.setattr(module, "ensure_connected", connected)


def make_client(spelling):
    """Session knowing the id peers; impostors: a chat titled like the string, a
    contact whose phone is the digits."""
    client = TelegramClient(StringSession(), 12345, "dummy_hash")
    impostor = types.Channel(
        id=IMPOSTOR_ID,
        title=spelling.strip(),
        photo=types.ChatPhotoEmpty(),
        date=None,
        access_hash=7,
        megagroup=True,
    )
    client.session.process_entities(
        SimpleNamespace(users=[PHONE_USER, SMALL], chats=[CHAN, impostor])
    )
    client.requests = []

    async def call(sender, request, ordered=False, flood_sleep_threshold=None):
        client.requests.append(request)
        if isinstance(request, GetContacts):
            return types.contacts.Contacts(contacts=[], saved_count=0, users=[PHONE_OWNER])
        if isinstance(request, functions.users.GetUsersRequest):
            known = {u.id: u for u in (PHONE_USER, SMALL, PHONE_OWNER)}
            return [known[i.user_id] for i in request.id if getattr(i, "user_id", 0) in known]
        if isinstance(request, functions.channels.GetChannelsRequest):
            return types.messages.Chats(chats=[CHAN])
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            return types.messages.DialogFilters(filters=[], tags_enabled=False)
        if isinstance(request, functions.messages.GetDialogsRequest):
            return types.messages.Dialogs(dialogs=[], messages=[], chats=[], users=[])
        if isinstance(request, functions.messages.UpdateDialogFilterRequest):
            return True
        if isinstance(request, functions.messages.ForwardMessagesRequest):
            return types.Updates(updates=[], users=[], chats=[], date=None, seq=0)
        if isinstance(request, functions.users.GetFullUserRequest):
            raise ValueError("stop after resolution")
        raise AssertionError(f"unexpected request {request!r}")

    client._call = call
    return client


def peer_of(client, request_type):
    sent = [r for r in client.requests if isinstance(r, request_type)]
    assert sent, f"{request_type.__name__} never sent"
    return sent


def assert_id_peer(peer, field, value):
    peer = peer if not hasattr(peer, "peer") else peer.peer
    assert getattr(peer, field, None) == value


def assert_untouched_impostors(client):
    assert not [r for r in client.requests if isinstance(r, GetContacts)]
    for r in client.requests:
        for p in (getattr(r, "id", None), getattr(r, "peer", None)):
            for item in p if isinstance(p, list) else [p]:
                assert getattr(item, "channel_id", None) != IMPOSTOR_ID
                assert getattr(item, "user_id", None) != PHONE_OWNER.id


@pytest.fixture
def use(monkeypatch):
    def _use(client):
        for module in (runtime, messages, profile, folders):
            monkeypatch.setattr(module, "get_client", lambda account=None: client)

    return _use


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", list(CASES))
async def test_resolve_input_entity_digit_string_is_id(spelling):
    field, value = CASES[spelling]
    client = make_client(spelling)
    peer = await runtime.resolve_input_entity(spelling, client)
    assert getattr(peer, field) == value
    assert_untouched_impostors(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", list(CASES))
async def test_resolve_entity_digit_string_is_id(spelling):
    _, value = CASES[spelling]
    client = make_client(spelling)
    entity = await runtime.resolve_entity(spelling, client)
    assert entity.id == value
    assert_untouched_impostors(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", list(CASES))
async def test_create_folder_digit_string_is_id(use, spelling):
    field, value = CASES[spelling]
    client = make_client(spelling)
    use(client)
    out = await folders.create_folder(title="T", chat_ids=[spelling])
    assert '"success": true' in out
    (req,) = peer_of(client, functions.messages.UpdateDialogFilterRequest)
    (included,) = req.filter.include_peers
    assert getattr(included, field) == value
    assert_untouched_impostors(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", list(CASES))
async def test_forward_message_send_as_digit_string_is_id(use, spelling):
    field, value = CASES[spelling]
    client = make_client(spelling)
    use(client)
    await messages.forward_message(
        from_chat_id=101, message_id=1, to_chat_id=101, send_as=spelling, expand_album=False
    )
    (req,) = peer_of(client, functions.messages.ForwardMessagesRequest)
    assert getattr(req.send_as, field) == value
    assert_untouched_impostors(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["15551234567", " 101 "])
async def test_get_full_user_digit_string_is_id(use, spelling):
    _, value = CASES[spelling]
    client = make_client(spelling)
    use(client)
    await profile.get_full_user(username=spelling)
    (req,) = peer_of(client, functions.users.GetFullUserRequest)
    assert req.id.id == value
    assert_untouched_impostors(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver", ["resolve_entity", "resolve_input_entity"])
@pytest.mark.parametrize("spelling", ["-1001234567890", "15551234567", " 101 "])
async def test_allowlist_still_converts_digit_strings(monkeypatch, resolver, spelling):
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", {"*"})
    seen = []

    class Client:
        async def get_entity(self, x):
            seen.append(x)
            return types.InputPeerSelf()

        async def get_input_entity(self, x):
            seen.append(x)
            return types.InputPeerSelf()

    await getattr(runtime, resolver)(spelling, Client())
    assert seen == [int(spelling.strip())]
