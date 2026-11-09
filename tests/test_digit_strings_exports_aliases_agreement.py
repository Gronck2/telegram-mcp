"""Task 0006 round 3 tester pass: digit strings in tools that skip validate_id
(exports), alias interplay, and agreement of both resolvers with the equivalent int."""

import json
from types import SimpleNamespace

import pytest
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession

from telegram_mcp import runtime
from telegram_mcp.tools import contacts, messages

GetContacts = functions.contacts.GetContactsRequest
USER = types.User(id=101, first_name="Small", access_hash=6)
CHAN = types.Channel(
    id=1234567890,
    title="Real",
    photo=types.ChatPhotoEmpty(),
    date=None,
    access_hash=9,
    megagroup=True,
)
BASIC = types.Chat(
    id=123, title="Basic", photo=types.ChatPhotoEmpty(), participants_count=2, date=None, version=1
)
OWNER = types.User(id=555, first_name="Owner", phone="101", access_hash=3)


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

    for module in (runtime, messages, contacts):
        monkeypatch.setattr(module, "ensure_connected", connected)


def make_client(title):
    client = TelegramClient(StringSession(), 12345, "dummy_hash")
    impostor = types.Channel(
        id=1001,
        title=title,
        photo=types.ChatPhotoEmpty(),
        date=None,
        access_hash=7,
        megagroup=True,
    )
    client.session.process_entities(SimpleNamespace(users=[USER], chats=[CHAN, BASIC, impostor]))
    client.requests = []

    async def call(sender, request, ordered=False, flood_sleep_threshold=None):
        client.requests.append(request)
        if isinstance(request, GetContacts):
            return types.contacts.Contacts(contacts=[], saved_count=0, users=[OWNER])
        if isinstance(request, functions.users.GetUsersRequest):
            return [USER for i in request.id if getattr(i, "user_id", 0) == USER.id]
        if isinstance(request, functions.channels.GetChannelsRequest):
            return types.messages.Chats(chats=[CHAN])
        if isinstance(request, functions.messages.GetChatsRequest):
            return types.messages.Chats(chats=[BASIC])
        if isinstance(request, functions.messages.GetDialogsRequest):
            return types.messages.Dialogs(dialogs=[], messages=[], chats=[], users=[])
        if isinstance(request, functions.messages.GetHistoryRequest):
            return types.messages.Messages(messages=[], chats=[], users=[], topics=[])
        raise AssertionError(f"unexpected request {request!r}")

    client._call = call
    return client


def clean(client):
    assert not [r for r in client.requests if isinstance(r, GetContacts)]
    for r in client.requests:
        item = getattr(r, "peer", None)
        assert getattr(item, "channel_id", None) != 1001


@pytest.fixture
def use(monkeypatch):
    def _use(client):
        for module in (runtime, messages, contacts):
            monkeypatch.setattr(module, "get_client", lambda account=None: client)

    return _use


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["-1001234567890", " 101 ", "-123"])
async def test_export_chat_history_digit_string_is_id(use, tmp_path, monkeypatch, spelling):
    client = make_client(spelling.strip())
    use(client)
    out = tmp_path / "e.json"

    async def writable(*a, **k):
        return out, None

    monkeypatch.setattr(messages, "_resolve_writable_file_path", writable)
    ret = await messages.export_chat_history(chat_id=spelling, output_path=str(out))
    assert out.exists(), ret
    data = json.loads(out.read_text())
    expected = int(spelling.strip())
    assert [c["chat_id"] for c in data["chats"].values()] == [expected]
    clean(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["-1001234567890", " 101 "])
async def test_export_unread_messages_digit_string_is_id(use, tmp_path, monkeypatch, spelling):
    client = make_client(spelling.strip())
    use(client)
    out = tmp_path / "u.json"

    async def writable(*a, **k):
        return out, None

    monkeypatch.setattr(messages, "_resolve_writable_file_path", writable)
    seen = []
    real = runtime.resolve_entity

    async def spy(identifier, cl=None):
        seen.append(identifier)
        return await real(identifier, cl)

    monkeypatch.setattr(messages, "resolve_entity", spy)
    await messages.export_unread_messages(chat_ids=[spelling], output_path=str(out))
    assert seen
    data = json.loads(out.read_text())
    assert [c["chat_id"] for c in data["chats"].values()] == [int(spelling.strip())]
    clean(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["-1001234567890", "-123", "101", " 101 "])
async def test_resolvers_agree_with_equivalent_int(ref):
    as_int = int(ref.strip())
    for resolver in ("resolve_entity", "resolve_input_entity"):
        a, b = make_client(ref.strip()), make_client(ref.strip())
        got_str = await getattr(runtime, resolver)(ref, a)
        got_int = await getattr(runtime, resolver)(as_int, b)
        assert got_str.to_dict() == got_int.to_dict()
        assert [type(r) for r in a.requests] == [type(r) for r in b.requests]
        clean(a)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["101", "-100123", "12345678", " 7 "])
async def test_set_contact_alias_refuses_numeric_keys(use, key):
    use(make_client("x"))
    out = await contacts.set_contact_alias(alias=key, chat_id=101)
    assert "alias_shadows_real_identifier" in out


@pytest.mark.asyncio
async def test_digit_string_not_shadowed_by_alias_file(tmp_path):
    # a hand-written alias file with a digit key must not redirect the digit string
    (tmp_path / "aliases.json").write_text(json.dumps({"101": 1234567890}))
    client = make_client("101")
    got = await runtime.resolve_entity("101", client)
    assert got.id == 101


@pytest.mark.asyncio
async def test_alias_still_resolves_exact_without_marked_guessing(tmp_path):
    (tmp_path / "aliases.json").write_text(
        json.dumps({"ivan petrov": {"id": 101, "name": "Ivan", "account": None}})
    )
    client = make_client("x")
    got = await runtime.resolve_entity("ivan petrov", client)
    assert got.id == 101
    # stale alias: no marked-variant guessing, user is asked instead
    (tmp_path / "aliases.json").write_text(
        json.dumps({"ivan petrov": {"id": 999, "name": "Ivan", "account": None}})
    )
    client = make_client("x")
    with pytest.raises(Exception) as info:
        await runtime.resolve_entity("ivan petrov", client)
    assert "ivan petrov" in str(info.value).lower()
    asked = [getattr(r, "id", None) for r in client.requests]
    flat = [getattr(i, "channel_id", None) for x in asked if isinstance(x, list) for i in x]
    assert -1000000000999 not in flat and 1000000000999 not in flat
