"""Under an allowlist both resolvers name the same peer (task 0004 review round 2)."""

import pytest
from telethon import functions, types

from telegram_mcp import runtime
from telegram_mcp.tools import folders, messages
from test_input_entity_guard import (  # noqa: F401
    IMPOSTOR_ID,
    REAL_ID,
    _channel,
    _clean,
    allow,
    make_client,
)
from test_input_entity_guard_callers import assert_real_peer_only, harness
from test_input_entity_guard_refs import _peer_key

NUMERIC_REAL_ID = 1234567890
NUMERIC_MARKED = "-1001234567890"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,call",
    [
        ("create_folder", lambda: folders.create_folder("work", chat_ids=[NUMERIC_MARKED])),
        (
            "forward_message_send_as",
            lambda: messages.forward_message(
                NUMERIC_MARKED, 5, NUMERIC_MARKED, send_as=NUMERIC_MARKED
            ),
        ),
    ],
)
async def test_numeric_string_title_never_reaches_impostor(monkeypatch, tool, call):
    allow(monkeypatch, NUMERIC_MARKED)
    client = make_client(title=NUMERIC_MARKED, rows=[_channel(NUMERIC_REAL_ID, "Real", hash_=5)])
    module = folders if tool == "create_folder" else messages
    sent = harness(monkeypatch, module, client)
    await call()
    joined = "\n".join(sent)
    assert f"'channel_id': {IMPOSTOR_ID}" not in joined
    assert f"'channel_id': {NUMERIC_REAL_ID}" in joined, sent


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", [NUMERIC_MARKED, " -1001234567890 ", "1234567890"])
async def test_numeric_strings_resolve_by_id(monkeypatch, ref):
    allow(monkeypatch, NUMERIC_MARKED)
    client = make_client(title=ref, rows=[_channel(NUMERIC_REAL_ID, "Real", hash_=5)])
    peer = await runtime.resolve_input_entity(ref, client)
    assert _peer_key(peer) == NUMERIC_REAL_ID
    assert client.requests == []


REFS = [
    "me",
    "Self",
    "@ME",
    " me",
    "foobar",
    "@FooBar",
    " @foobar ",
    "t.me/foobar",
    "https://telegram.me/foobar",
    "t.me/+abc",
    "t.me/joinchat/abc",
    "tg://join?invite=abc",
    "tg://resolve?domain=foobar",
    "@ foobar",
    "-1001234567890",
    "12345678",
    "+1 555 123",
    "15551234",
    "My Group",
    "abcde_",
    "@-1001234567890",
]


async def _outcome(resolver, ref, client):
    try:
        got = await getattr(runtime, resolver)(ref, client)
    except Exception:
        return "refused"
    if getattr(got, "is_self", False) or isinstance(got, types.InputPeerSelf):
        return "self"
    return _peer_key(got)


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", REFS)
async def test_resolvers_agree_and_skip_impostor(monkeypatch, ref):
    allow(monkeypatch, "foobar,-1001234567890")
    outcomes = []
    for resolver in ("resolve_entity", "resolve_input_entity"):
        client = make_client(title=ref, rows=[_channel(NUMERIC_REAL_ID, "Real", hash_=5)])

        async def call(sender, request, ordered=False, flood_sleep_threshold=None, c=client):
            c.requests.append(request)
            if isinstance(request, functions.contacts.ResolveUsernameRequest):
                return types.contacts.ResolvedPeer(
                    peer=types.PeerChannel(REAL_ID), chats=[c.real], users=[]
                )
            if isinstance(request, functions.contacts.GetContactsRequest):
                return types.contacts.Contacts(contacts=[], saved_count=0, users=[])
            if isinstance(request, functions.channels.GetChannelsRequest):
                return types.messages.Chats(chats=[_channel(NUMERIC_REAL_ID, "Real", hash_=5)])
            if isinstance(request, functions.users.GetUsersRequest):
                return [types.User(id=777, first_name="Owner", is_self=True, access_hash=4)]
            raise ValueError("no such thing")

        client._call = call
        outcomes.append(await _outcome(resolver, ref, client))
    assert outcomes[0] == outcomes[1], (ref, outcomes)
    if ref.strip().lstrip("@").strip().lower() in ("foobar", "-1001234567890"):
        assert IMPOSTOR_ID not in outcomes


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["t.me/+abc", "My Group", "tg://resolve?domain=x"])
async def test_no_allowlist_requests_unchanged(ref):
    guarded = make_client(title=ref)
    plain = make_client(title=ref)
    try:
        await runtime.resolve_input_entity(ref, guarded)
    except Exception:
        pass
    try:
        await plain.get_input_entity(ref)
    except Exception:
        pass
    assert [type(r) for r in guarded.requests] == [type(r) for r in plain.requests]
