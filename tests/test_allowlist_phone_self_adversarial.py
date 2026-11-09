"""Adversarial phone/self cases for allowlist resolution (task 0005, tester pass)."""

from types import SimpleNamespace

import pytest
from telethon import TelegramClient, functions, types, utils
from telethon.sessions import StringSession

from telegram_mcp import runtime
from telegram_mcp.tools import messages

DIGITS = "15551234567"
SPELLINGS = ["+1 555 123 4567", "+1-555-123-4567", "+15551234567", "@+15551234567"]
ME = types.User(id=555, first_name="Me", is_self=True, access_hash=1)
PEER = types.User(id=700, first_name="Peer", phone=DIGITS, access_hash=2)
OTHER = types.User(id=701, first_name="Other", phone="19998887777", access_hash=3)


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
    monkeypatch.setattr(messages, "ensure_connected", connected)


def make_client(title=None, contacts=(), rows=()):
    client = TelegramClient(StringSession(), 12345, "dummy_hash")
    chats = []
    if title is not None:
        chats.append(
            types.Channel(
                id=1001,
                title=title,
                photo=types.ChatPhotoEmpty(),
                date=None,
                access_hash=7,
                megagroup=True,
            )
        )
    client.session.process_entities(SimpleNamespace(users=list(rows), chats=chats))
    client.requests = []

    async def call(sender, request, ordered=False, flood_sleep_threshold=None):
        client.requests.append(request)
        if isinstance(request, functions.contacts.GetContactsRequest):
            return types.contacts.Contacts(contacts=[], saved_count=0, users=list(contacts))
        if isinstance(request, functions.users.GetUsersRequest):
            return [ME]
        if isinstance(request, functions.messages.SendMessageRequest):
            return types.Updates(updates=[], users=[], chats=[], date=None, seq=0)
        raise AssertionError(f"unexpected request {request!r}")

    client._call = call
    return client


def allow(monkeypatch, *entries):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", ",".join(str(e) for e in entries))


async def outcome(getter, string, client):
    """Entity id (as a comparable key) or the string 'refused'."""
    try:
        got = await getattr(runtime, getter)(string, client)
    except (ValueError, runtime.AliasNeedsUser):
        return "refused"
    if isinstance(got, types.InputPeerSelf):
        return "self"
    return getattr(got, "id", None) or getattr(got, "user_id", None) or getattr(got, "channel_id")


@pytest.mark.asyncio
@pytest.mark.parametrize("getter", ["resolve_entity", "resolve_input_entity"])
@pytest.mark.parametrize("spelling", SPELLINGS)
async def test_display_name_equal_to_spelling_never_answers(monkeypatch, getter, spelling):
    allow(monkeypatch, "+15551234567")
    client = make_client(title=spelling)
    assert await outcome(getter, spelling, client) == "refused"


@pytest.mark.asyncio
@pytest.mark.parametrize("getter", ["resolve_entity", "resolve_input_entity"])
@pytest.mark.parametrize("spelling", SPELLINGS[:3])
async def test_every_spelling_resolves_same_contact(monkeypatch, getter, spelling):
    allow(monkeypatch, "+15551234567")
    client = make_client(title=spelling, contacts=[PEER])
    assert await outcome(getter, spelling, client) == PEER.id


@pytest.mark.asyncio
@pytest.mark.parametrize("getter", ["resolve_entity", "resolve_input_entity"])
async def test_digits_only_is_an_id_not_a_phone_or_name(monkeypatch, getter):
    allow(monkeypatch, "+15551234567")
    client = make_client(title=DIGITS, contacts=[PEER])
    result = await outcome(getter, DIGITS, client)
    assert result != 1001 and result != PEER.id
    assert not [r for r in client.requests if isinstance(r, functions.contacts.GetContactsRequest)]


@pytest.mark.asyncio
@pytest.mark.parametrize("getter", ["resolve_entity", "resolve_input_entity"])
@pytest.mark.parametrize("stored", ["15551234567", "+15551234567"])
async def test_contact_phone_stored_with_or_without_plus_never_hits_name(
    monkeypatch, getter, stored
):
    allow(monkeypatch, "+15551234567")
    contact = types.User(id=700, first_name="P", phone=stored, access_hash=2)
    client = make_client(title="+15551234567", contacts=[contact])
    result = await outcome(getter, "+15551234567", client)
    assert result in (700, "refused")
    assert result != 1001
    if stored == DIGITS:
        assert result == 700


@pytest.mark.asyncio
@pytest.mark.parametrize("getter", ["resolve_entity", "resolve_input_entity"])
async def test_contact_without_phone_is_skipped(monkeypatch, getter):
    allow(monkeypatch, "+15551234567")
    nophone = types.User(id=702, first_name="NoPhone", phone=None, access_hash=4)
    client = make_client(title="+15551234567", contacts=[nophone])
    assert await outcome(getter, "+15551234567", client) == "refused"


@pytest.mark.asyncio
async def test_phone_row_hit_of_non_listed_peer_is_refused_by_entity_check(monkeypatch):
    allow(monkeypatch, "+15551234567")
    client = make_client(rows=[OTHER])

    async def get_entity(peer):
        return OTHER

    client.get_entity = get_entity
    entity = await runtime.resolve_entity("+19998887777", client)
    assert entity.id == OTHER.id
    # Neither the string nor the resolved user is on the list.
    assert not runtime.is_chat_allowed("+19998887777", entity)
    assert not runtime.is_chat_allowed("+19998887777", entity, runtime.CHAT_ACCESS_WRITE)


@pytest.mark.asyncio
@pytest.mark.parametrize("string", SPELLINGS[:3])
@pytest.mark.parametrize("cached", ["row", "contact", "none"])
async def test_agreement_input_equals_peer_of_entity_or_both_refuse(monkeypatch, string, cached):
    allow(monkeypatch, "+15551234567")
    client = make_client(
        title=string,
        rows=[PEER] if cached == "row" else [],
        contacts=[PEER] if cached == "contact" else [],
    )
    if cached == "row":

        async def get_entity(peer):
            return PEER

        client.get_entity = get_entity
    try:
        entity = await runtime.resolve_entity(string, client)
    except ValueError:
        with pytest.raises(ValueError):
            await runtime.resolve_input_entity(string, client)
        assert cached == "none"
        return
    peer = await runtime.resolve_input_entity(string, client)
    assert peer == utils.get_input_peer(entity)


@pytest.mark.asyncio
@pytest.mark.parametrize("getter", ["resolve_entity", "resolve_input_entity"])
@pytest.mark.parametrize("string", ["+15551234567", "+1 555 123 4567"])
async def test_without_allowlist_phone_resolves_by_contact_never_by_title(
    monkeypatch, getter, string
):
    # task 0006 round 2: phones go through _phone_lookup with or without a list
    client = make_client(title=string, contacts=[PEER])
    got = await getattr(runtime, getter)(string, client)
    assert utils.get_peer_id(got) == PEER.id
    assert (
        len([r for r in client.requests if isinstance(r, functions.contacts.GetContactsRequest)])
        == 1
    )
    # no contact, only a chat titled like the phone: refused, not matched by title
    client = make_client(title=string)
    with pytest.raises(ValueError):
        await getattr(runtime, getter)(string, client)


@pytest.mark.asyncio
@pytest.mark.parametrize("getter", ["resolve_entity", "resolve_input_entity"])
@pytest.mark.parametrize("string", ["@foobar", "foobar", "t.me/foobar", "My Group", "me"])
async def test_non_phone_strings_never_call_get_contacts_under_allowlist(
    monkeypatch, getter, string
):
    allow(monkeypatch, "@foobar", 555)
    client = make_client(title=string)
    try:
        await getattr(runtime, getter)(string, client)
    except Exception:
        pass
    assert not [r for r in client.requests if isinstance(r, functions.contacts.GetContactsRequest)]


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["me", "self", "Me", "SELF"])
@pytest.mark.parametrize("getter", ["resolve_entity", "resolve_input_entity"])
async def test_self_callers_resolve_to_own_account(monkeypatch, caller, getter):
    allow(monkeypatch, 555)
    client = make_client(title=caller)
    got = await getattr(runtime, getter)(caller, client)
    assert isinstance(got, types.InputPeerSelf) or getattr(got, "is_self", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["me", "self", "Me", "@me", "@Me"])
@pytest.mark.parametrize("listed", [True, False])
async def test_send_message_to_self_needs_own_id_on_list(monkeypatch, caller, listed):
    allow(monkeypatch, 555 if listed else 999)
    client = make_client(title=caller)
    monkeypatch.setattr(messages, "get_client", lambda account=None: client)
    monkeypatch.setattr(runtime, "get_client", lambda account=None: client)
    out = await messages.send_message(chat_id=caller, message="hi")
    sent = [r for r in client.requests if isinstance(r, functions.messages.SendMessageRequest)]
    if listed and not caller.startswith("@"):
        assert len(sent) == 1, out
        assert isinstance(sent[0].peer, (types.InputPeerSelf, types.User, types.InputPeerUser))
        if isinstance(sent[0].peer, types.User):
            assert sent[0].peer.id == 555
    else:
        # "@me" is an ordinary username lookup, never the own account: always refused.
        assert not sent, out
        assert "restricted" in out or "occurred" in out or "Could not" in out, out
