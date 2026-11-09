"""Phone entries resolve only via contacts/phone cache; self entries are inert (task 0005)."""

from types import SimpleNamespace

import pytest
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession

from telegram_mcp import runtime
from telegram_mcp.tools import contacts, messages

PHONE = "+15551234567"
IMPOSTOR = types.Channel(
    id=1001, title=PHONE, photo=types.ChatPhotoEmpty(), date=None, access_hash=7, megagroup=True
)
DEST = types.Channel(
    id=1002, title="Dest", photo=types.ChatPhotoEmpty(), date=None, access_hash=8, megagroup=True
)
DEST_ID = -1000000001002
OWNER = types.User(id=555, first_name="Owner", phone="15551234567", access_hash=3)


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

    monkeypatch.setattr(runtime, "ensure_connected", connected)
    monkeypatch.setattr(contacts, "ensure_connected", connected)
    monkeypatch.setattr(messages, "ensure_connected", connected)


def make_client(contact=None):
    client = TelegramClient(StringSession(), 12345, "dummy_hash")
    client.session.process_entities(SimpleNamespace(users=[], chats=[IMPOSTOR, DEST]))
    client.requests = []

    async def call(sender, request, ordered=False, flood_sleep_threshold=None):
        client.requests.append(request)
        if isinstance(request, functions.contacts.GetContactsRequest):
            users = [contact] if contact else []
            return types.contacts.Contacts(contacts=[], saved_count=0, users=users)
        if isinstance(request, functions.channels.GetChannelsRequest):
            return types.messages.Chats(chats=[DEST])
        if isinstance(request, functions.messages.ForwardMessagesRequest):
            return types.Updates(updates=[], users=[], chats=[], date=None, seq=0)
        raise AssertionError(f"unexpected request {request!r}")

    client._call = call
    return client


def use(monkeypatch, client):
    for module in (runtime, contacts, messages):
        monkeypatch.setattr(module, "get_client", lambda account=None: client)


def allow(monkeypatch, value=PHONE):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", f"{value},{DEST_ID}")


@pytest.mark.asyncio
@pytest.mark.parametrize("getter", ["resolve_entity", "resolve_input_entity"])
@pytest.mark.parametrize("string", [PHONE, "+1 (555) 123-4567"])
async def test_phone_never_resolves_to_chat_named_like_it(monkeypatch, getter, string):
    allow(monkeypatch)
    client = make_client()
    with pytest.raises(ValueError):
        await getattr(runtime, getter)(string, client)


@pytest.mark.asyncio
async def test_without_allowlist_phone_still_falls_back_to_names():
    client = make_client()
    peer = await runtime.resolve_input_entity(PHONE, client)
    assert peer.channel_id == IMPOSTOR.id


@pytest.mark.asyncio
async def test_contact_with_the_phone_still_resolves(monkeypatch):
    allow(monkeypatch)
    client = make_client(contact=OWNER)
    entity = await runtime.resolve_entity(PHONE, client)
    peer = await runtime.resolve_input_entity(PHONE, client)
    assert entity.id == OWNER.id
    assert peer == runtime.utils.get_input_peer(entity)


@pytest.mark.asyncio
async def test_phone_row_hit_still_resolves(monkeypatch):
    allow(monkeypatch)
    client = make_client()
    client.session.process_entities(SimpleNamespace(users=[OWNER], chats=[]))

    async def get_entity(peer):
        assert isinstance(peer, types.InputPeerUser)
        return OWNER

    client.get_entity = get_entity
    assert (await runtime.resolve_input_entity(PHONE, client)).user_id == OWNER.id
    assert (await runtime.resolve_entity(PHONE, client)).id == OWNER.id


@pytest.mark.asyncio
async def test_set_contact_alias_never_binds_chat_named_like_phone(monkeypatch):
    allow(monkeypatch)
    use(monkeypatch, make_client())
    await contacts.set_contact_alias("my boss", PHONE)
    assert "my boss" not in runtime.load_aliases()


@pytest.mark.asyncio
async def test_forward_send_as_never_uses_chat_named_like_phone(monkeypatch):
    allow(monkeypatch)
    client = make_client()
    use(monkeypatch, client)
    out = await messages.forward_message(
        from_chat_id=DEST_ID, message_id=1, to_chat_id=DEST_ID, send_as=PHONE, expand_album=False
    )
    assert "occurred" in out or "Could not" in out, out
    assert not [r for r in client.requests if type(r).__name__ == "ForwardMessagesRequest"], out


@pytest.mark.asyncio
async def test_forward_send_as_contact_phone_works(monkeypatch):
    allow(monkeypatch, f"{PHONE},{OWNER.id}")
    client = make_client(contact=OWNER)
    use(monkeypatch, client)
    out = await messages.forward_message(
        from_chat_id=DEST_ID, message_id=1, to_chat_id=DEST_ID, send_as=PHONE, expand_album=False
    )
    assert "occurred" not in out, out
    [request] = [r for r in client.requests if type(r).__name__ == "ForwardMessagesRequest"]
    assert request.send_as.user_id == OWNER.id


@pytest.mark.parametrize("entry", ["self", "@self", "t.me/self", "SELF", "@Self"])
def test_self_entry_is_inert_with_one_warning(capsys, entry):
    allowed = runtime._parse_allowed_chat_ids(entry)
    assert allowed == {runtime._INERT_CHAT_ENTRY}
    assert capsys.readouterr().err.count("WARNING: ignoring") == 1


@pytest.mark.parametrize("caller", ["self", "SELF", "@self", " Self "])
def test_self_caller_not_granted_by_string(caller):
    assert not runtime._identifier_matches_allowlist({"self", 555}, caller)
    assert not runtime._identifier_matches_allowlist({runtime._INERT_CHAT_ENTRY}, caller)


def test_own_id_entry_still_grants_by_id():
    allowed = runtime._parse_allowed_chat_ids("self,555")
    assert runtime._identifier_matches_allowlist(allowed, 555)
    assert runtime._identifier_matches_allowlist(allowed, "555")
    assert not runtime._identifier_matches_allowlist(allowed, "self")
