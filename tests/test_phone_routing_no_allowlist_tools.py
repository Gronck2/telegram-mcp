"""Phone strings through real tools WITHOUT an allowlist (task 0006, round 2 tester pass).

A phone is answered by the phone cache or the contacts list, never by a chat title; bare
digit strings, usernames, aliases and 'me' keep their old Telethon path (no GetContacts).
"""

from types import SimpleNamespace

import pytest
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession

from telegram_mcp import runtime
from telegram_mcp.tools import groups, messages

PHONE = "+15551234567"
IMPOSTOR_ID = 1001
CONTACT = types.User(id=555, first_name="Owner", phone="15551234567", access_hash=3)
ALICE = types.User(id=777, first_name="Alice", username="alice", access_hash=4)
SMALL = types.User(id=101, first_name="Small", access_hash=6)
BIG = types.User(id=12345678, first_name="Big", access_hash=8)
CHAN = types.Channel(
    id=123, title="Chan", photo=types.ChatPhotoEmpty(), date=None, access_hash=9, megagroup=True
)
SPELLINGS = [PHONE, " +15551234567 ", "+1 555 123 4567", "+1-555-123-4567"]
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

    for module in (runtime, messages, groups):
        monkeypatch.setattr(module, "ensure_connected", connected)


def make_client(title=None, contact=None):
    client = TelegramClient(StringSession(), 12345, "dummy_hash")
    chats = [CHAN]
    if title is not None:
        chats.append(
            types.Channel(
                id=IMPOSTOR_ID,
                title=title,
                photo=types.ChatPhotoEmpty(),
                date=None,
                access_hash=7,
                megagroup=True,
            )
        )
    client.session.process_entities(SimpleNamespace(users=[ALICE, SMALL, BIG], chats=chats))
    client.requests = []

    async def call(sender, request, ordered=False, flood_sleep_threshold=None):
        client.requests.append(request)
        if isinstance(request, GetContacts):
            users = [contact] if contact else []
            return types.contacts.Contacts(contacts=[], saved_count=0, users=users)
        if isinstance(request, functions.users.GetUsersRequest):
            known = {u.id: u for u in (CONTACT, ALICE, SMALL, BIG)}
            return [
                known[getattr(i, "user_id", 777)]
                for i in request.id
                if getattr(i, "user_id", 777) in known
            ]
        if isinstance(request, functions.messages.GetMessagesRequest):
            return types.messages.Messages(messages=[], chats=[], users=[])
        if isinstance(request, functions.channels.GetParticipantsRequest):
            return types.channels.ChannelParticipants(count=0, participants=[], chats=[], users=[])
        if isinstance(request, functions.contacts.ResolveUsernameRequest):
            return types.contacts.ResolvedPeer(
                peer=types.PeerUser(ALICE.id), chats=[], users=[ALICE]
            )
        if isinstance(
            request,
            (
                functions.messages.SendReactionRequest,
                functions.messages.SaveDraftRequest,
                functions.messages.SetTypingRequest,
            ),
        ):
            return types.Updates(updates=[], users=[], chats=[], date=None, seq=0)
        if isinstance(request, functions.messages.GetDialogsRequest):
            return types.messages.Dialogs(dialogs=[], messages=[], chats=[], users=[])
        raise AssertionError(f"unexpected request {request!r}")

    client._call = call
    return client


def use(monkeypatch, client):
    for module in (runtime, messages, groups):
        monkeypatch.setattr(module, "get_client", lambda account=None: client)


def _flatten(value):
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _flatten(item)
    elif value is not None:
        yield value


def ids_touched(client):
    out = set()
    for r in client.requests:
        for attr in ("peer", "id", "channel", "user_id"):
            for p in _flatten(getattr(r, attr, None)):
                for field in ("user_id", "channel_id", "chat_id", "id"):
                    v = getattr(p, field, None)
                    if isinstance(v, int):
                        out.add(v)
    return out


def contacts_calls(client):
    return [r for r in client.requests if isinstance(r, GetContacts)]


async def run_tool(name, chat_id):
    if name == "send_reaction":
        return await messages.send_reaction(chat_id=chat_id, message_id=1, emoji="x")
    if name == "save_draft":
        return await messages.save_draft(chat_id=chat_id, message="hello")
    if name == "send_typing":
        return await messages.send_typing(chat_id=chat_id, seconds=0.5)
    if name == "get_message_reactions":
        return await messages.get_message_reactions(chat_id=chat_id, message_id=1)
    if name == "get_admins":
        return await groups.get_admins(chat_id=chat_id)
    raise AssertionError(name)


TOOLS = ["send_reaction", "save_draft", "send_typing", "get_message_reactions", "get_admins"]
REQUEST_TYPE = {
    "send_reaction": functions.messages.SendReactionRequest,
    "save_draft": functions.messages.SaveDraftRequest,
    "send_typing": functions.messages.SetTypingRequest,
    "get_message_reactions": functions.messages.GetMessagesRequest,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("spelling", SPELLINGS)
async def test_impostor_title_no_contact_is_refused(monkeypatch, tool, spelling):
    client = make_client(title=spelling.strip())
    use(monkeypatch, client)
    out = await run_tool(tool, spelling)
    assert IMPOSTOR_ID not in ids_touched(client)
    assert "error occurred" in out.lower() or "error" in out.lower() or "not found" in out.lower()
    assert len(contacts_calls(client)) >= 1
    for request_type in REQUEST_TYPE.values():
        assert not [r for r in client.requests if isinstance(r, request_type)]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("spelling", SPELLINGS)
async def test_impostor_title_with_contact_resolves_contact(monkeypatch, tool, spelling):
    client = make_client(title=spelling.strip(), contact=CONTACT)
    use(monkeypatch, client)
    await run_tool(tool, spelling)
    touched = ids_touched(client)
    assert IMPOSTOR_ID not in touched
    if tool != "get_message_reactions":
        assert CONTACT.id in touched
    if tool in ("send_reaction", "save_draft", "send_typing"):
        sent = [r for r in client.requests if isinstance(r, REQUEST_TYPE[tool])]
        assert sent and all(r.peer.user_id == CONTACT.id for r in sent)
        if tool != "send_typing":
            assert len(sent) == 1
    else:
        assert contacts_calls(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["+1 555 123 4567", PHONE])
async def test_phone_in_phone_cache_wins_without_contacts_call(monkeypatch, spelling):
    client = make_client(title=PHONE)
    client.session.process_entities(
        SimpleNamespace(
            users=[types.User(id=555, first_name="Owner", phone="15551234567", access_hash=3)],
            chats=[],
        )
    )
    use(monkeypatch, client)
    await run_tool("send_reaction", spelling)
    sent = [r for r in client.requests if isinstance(r, functions.messages.SendReactionRequest)]
    assert len(sent) == 1 and sent[0].peer.user_id == 555
    assert contacts_calls(client) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("getter", ["get_entity", "get_input_entity"])
@pytest.mark.parametrize("ref", ["101", "12345678", "-100123", " 101 "])
async def test_bare_digit_strings_keep_telethon_calls(getter, ref):
    # Telethon itself may ask GetContacts for a digit string; the point is that the
    # resolver adds nothing before it: the raw call sequence is a prefix of ours.
    routed, raw = make_client(title=ref.strip()), make_client(title=ref.strip())
    resolver = {
        "get_entity": runtime.resolve_entity,
        "get_input_entity": runtime.resolve_input_entity,
    }
    try:
        await resolver[getter](ref, routed)
    except ValueError:
        pass
    try:
        await getattr(raw, getter)(ref)
    except ValueError:
        pass
    ours = [type(r) for r in routed.requests]
    theirs = [type(r) for r in raw.requests]
    assert ours[: len(theirs)] == theirs
    assert IMPOSTOR_ID not in ids_touched(routed) or IMPOSTOR_ID in ids_touched(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("ref, expected", [("101", 101), ("12345678", 12345678)])
async def test_bare_digit_ids_resolve_as_ids_in_tool(monkeypatch, ref, expected):
    client = make_client()
    use(monkeypatch, client)
    await run_tool("send_reaction", ref)
    sent = [r for r in client.requests if isinstance(r, functions.messages.SendReactionRequest)]
    assert len(sent) == 1 and sent[0].peer.user_id == expected
    assert contacts_calls(client) == []


@pytest.mark.asyncio
async def test_negative_channel_style_id_unchanged(monkeypatch):
    client = make_client()
    use(monkeypatch, client)
    await run_tool("send_reaction", "-1000000000123")
    sent = [r for r in client.requests if isinstance(r, functions.messages.SendReactionRequest)]
    assert len(sent) == 1 and sent[0].peer.channel_id == 123
    assert contacts_calls(client) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["alice", "@alice", "ALICE"])
async def test_usernames_same_calls_as_raw_telethon(name):
    routed, raw = make_client(), make_client()
    got = await runtime.resolve_input_entity(name, routed)
    want = await raw.get_input_entity(name)
    assert got == want
    assert [type(r) for r in routed.requests] == [type(r) for r in raw.requests]
    assert contacts_calls(routed) == []


@pytest.mark.asyncio
async def test_uncached_username_hits_resolve_username_not_contacts():
    routed, raw = make_client(), make_client()
    got = await runtime.resolve_input_entity("@bob_not_cached", routed)
    want = await raw.get_input_entity("@bob_not_cached")
    assert got == want
    assert [type(r) for r in routed.requests] == [type(r) for r in raw.requests]
    assert contacts_calls(routed) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("getter", ["resolve_entity", "resolve_input_entity"])
@pytest.mark.parametrize("me", ["me", "self"])
async def test_me_unchanged(getter, me):
    routed, raw = make_client(), make_client()
    method = {"resolve_entity": "get_entity", "resolve_input_entity": "get_input_entity"}[getter]
    got = await getattr(runtime, getter)(me, routed)
    want = await getattr(raw, method)(me)
    assert type(got) is type(want)
    assert [type(r) for r in routed.requests] == [type(r) for r in raw.requests]
    assert contacts_calls(routed) == []


@pytest.mark.asyncio
async def test_alias_resolves_through_id_path(monkeypatch):
    monkeypatch.setattr(runtime, "apply_alias", lambda v: 777 if v == "my friend" else v)
    client = make_client()
    use(monkeypatch, client)
    await run_tool("send_reaction", "my friend")
    sent = [r for r in client.requests if isinstance(r, functions.messages.SendReactionRequest)]
    assert len(sent) == 1 and sent[0].peer.user_id == 777
    assert contacts_calls(client) == []
