"""Self references, links and ``@``-prefixed ids under an allowlist (task 0004 review round 1)."""

from types import SimpleNamespace

import pytest
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession

from telegram_mcp import runtime
from telegram_mcp.tools import messages
from test_input_entity_guard import (  # noqa: F401
    IMPOSTOR_ID,
    REAL_ID,
    _channel,
    _clean,
    allow,
    make_client,
    peer_id,
)
from test_input_entity_guard_callers import _Stop, harness

SELF_ID = 777
NAMED_ME_ID = 4242


def _me_client():
    """A client whose cache holds a user whose first name is "Me" next to the real self."""
    client = TelegramClient(StringSession(), 12345, "dummy_hash")
    impostor = types.User(id=NAMED_ME_ID, first_name="Me", access_hash=3)
    client.session.process_entities(SimpleNamespace(users=[impostor], chats=[]))
    me = types.User(id=SELF_ID, first_name="Owner", is_self=True, access_hash=4)
    client.requests = []

    async def call(sender, request, ordered=False, flood_sleep_threshold=None):
        client.requests.append(request)
        if isinstance(request, functions.users.GetUsersRequest):
            return [me]
        raise _Stop()

    client._call = call
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["Me", "SELF", "Self", "me", "self"])
async def test_self_ref_any_case_is_own_account(monkeypatch, ref):
    allow(monkeypatch, str(SELF_ID))
    client = _me_client()
    peer = await runtime.resolve_input_entity(ref, client)
    assert isinstance(peer, types.InputPeerSelf)


@pytest.mark.asyncio
async def test_self_ref_case_variant_without_allowlist_unchanged():
    client = _me_client()
    peer = await runtime.resolve_input_entity("Me", client)
    assert isinstance(peer, types.InputPeerUser) and peer.user_id == NAMED_ME_ID


@pytest.mark.asyncio
async def test_send_reaction_me_never_hits_impostor(monkeypatch):
    allow(monkeypatch, str(SELF_ID))
    client = _me_client()
    monkeypatch.setattr(messages, "get_client", lambda account=None: client)

    async def connected(cl):
        return None

    monkeypatch.setattr(messages, "ensure_connected", connected)
    await messages.send_reaction("Me", 5, "\U0001f44d")
    sends = [r for r in client.requests if isinstance(r, functions.messages.SendReactionRequest)]
    assert len(sends) == 1
    assert isinstance(sends[0].peer, types.InputPeerSelf)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "link,title",
    [
        ("https://t.me/foobar", "https://t.me/foobar"),
        ("t.me/foobar/", "t.me/foobar/"),
        ("@abcd", "@abcd"),
        ("abcd", "abcd"),
    ],
)
@pytest.mark.parametrize("resolver", ["resolve_entity", "resolve_input_entity"])
async def test_link_and_short_handle_never_hit_impostor(monkeypatch, link, title, resolver):
    allow(monkeypatch, "foobar")
    client = make_client(title=title)
    peer = await getattr(runtime, resolver)(link, client)
    assert getattr(peer, "channel_id", None) == REAL_ID or peer.id == REAL_ID
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_forward_send_as_link_acts_on_real_peer(monkeypatch):
    allow(monkeypatch, "foobar")
    client = make_client(title="https://t.me/foobar")
    sent = harness(monkeypatch, messages, client)
    await messages.forward_message("@foobar", 5, "@foobar", send_as="https://t.me/foobar")
    # either refused or sent to the real peer; the title-colliding chat is never used
    assert f"'channel_id': {IMPOSTOR_ID}" not in "\n".join(sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("handle", ["@me", "@12345", "@-100123", "@Me"])
@pytest.mark.parametrize("resolver", ["resolve_entity", "resolve_input_entity"])
async def test_prefixed_ids_and_self_refs_refused_under_allowlist(monkeypatch, handle, resolver):
    allow(monkeypatch, "foobar")
    client = make_client(title=handle, rows=[_channel(3003, handle, hash_=5)])
    with pytest.raises(ValueError, match="not a username or an id"):
        await getattr(runtime, resolver)(handle, client)
    assert client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("handle", ["@me", "@12345", "@-100123"])
async def test_prefixed_ids_unchanged_without_allowlist(handle):
    client = make_client(title=handle)
    peer = await runtime.resolve_input_entity(handle, client)
    assert peer_id(peer) == IMPOSTOR_ID


def test_malformed_exemptions():
    for ok in ("me", "SELF", "12345678", "-100123", "+15551234567", "@validname"):
        assert runtime._is_malformed_handle(ok) is False
    for bad in ("@me", "@12345", "@-100123", "+abcde", "abcde_"):
        assert runtime._is_malformed_handle(bad) is True


# ---- round 2: edge strings, resolver agreement, no-allowlist parity -------------------


def _peer_key(peer):
    for attr in ("channel_id", "user_id", "id"):
        if getattr(peer, attr, None):
            return getattr(peer, attr)
    return type(peer).__name__


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ref", ["@self", " self", "SELF\n", "telegram.me/foobar", "https://telegram.me/foobar"]
)
@pytest.mark.parametrize("resolver", ["resolve_entity", "resolve_input_entity"])
async def test_self_handle_and_telegram_me_links_use_network_not_name(monkeypatch, ref, resolver):
    allow(monkeypatch, "foobar")
    client = make_client(title=ref, rows=[_channel(3003, ref, hash_=5)])
    peer = await getattr(runtime, resolver)(ref, client)
    assert _peer_key(peer) == REAL_ID
    assert [type(r) for r in client.requests] == [functions.contacts.ResolveUsernameRequest]


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", [" Me", "Me ", "me ", " Me\t"])
@pytest.mark.parametrize("resolver", ["resolve_entity", "resolve_input_entity"])
async def test_whitespace_self_refs_refused_under_allowlist(monkeypatch, ref, resolver):
    allow(monkeypatch, "foobar")
    client = make_client(title=ref.strip(), rows=[_channel(3003, ref, hash_=5)])
    with pytest.raises(ValueError, match="not a username or an id"):
        await getattr(runtime, resolver)(ref, client)
    assert client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", [" foobar", "foobar ", "@FOOBAR"])
async def test_resolvers_agree_on_padded_and_cased_handles(monkeypatch, ref):
    allow(monkeypatch, "foobar")
    keys = []
    for resolver in ("resolve_entity", "resolve_input_entity"):
        client = make_client(title=ref)
        keys.append(_peer_key(await getattr(runtime, resolver)(ref, client)))
        assert len(client.requests) == 1
    assert keys == [REAL_ID, REAL_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ref",
    [
        "t.me/+abc",
        "t.me/joinchat/abc",
        "https://t.me/joinchat/abc",
        "tg://join?invite=abc",
        "tg://resolve?domain=foobar",
        "@ foobar",
    ],
)
async def test_invite_and_tg_links_never_reach_name_column(monkeypatch, ref):
    allow(monkeypatch, "foobar")
    client = make_client(title=ref)
    try:
        peer = await runtime.resolve_input_entity(ref, client)
    except Exception:
        return
    assert peer_id(peer) != IMPOSTOR_ID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ref", ["@self", "telegram.me/foobar", "Me ", " foobar", "@Me", "t.me/foobar"]
)
async def test_no_allowlist_parity_with_plain_telethon(ref):
    guarded = make_client(title=ref)
    plain = make_client(title=ref)
    try:
        got = _peer_key(await runtime.resolve_input_entity(ref, guarded))
    except Exception as error:
        got = type(error)
    try:
        want = _peer_key(await plain.get_input_entity(ref))
    except Exception as error:
        want = type(error)
    assert got == want
    assert [type(r) for r in guarded.requests] == [type(r) for r in plain.requests]
