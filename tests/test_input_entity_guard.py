"""Under an allowlist a handle never resolves to a chat found by display name (task 0004)."""

from types import SimpleNamespace

import pytest
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession

import main
from telegram_mcp import runtime
from telegram_mcp.tools import groups, messages

IMPOSTOR_ID = 1001
REAL_ID = 2002


def _channel(chan_id, title, username=None, hash_=7):
    return types.Channel(
        id=chan_id,
        title=title,
        photo=types.ChatPhotoEmpty(),
        date=None,
        username=username,
        access_hash=hash_,
        megagroup=True,
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for name in (
        "TELEGRAM_ALLOWED_CHAT_IDS",
        "TELEGRAM_ALLOWED_READ_CHATS",
        "TELEGRAM_ALLOWED_WRITE_CHATS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", None)
    monkeypatch.setattr(main, "ALLOWED_CHAT_IDS", None)
    monkeypatch.setenv("TELEGRAM_ALIASES_FILE", str(tmp_path / "aliases.json"))

    async def connected(client):
        return None

    monkeypatch.setattr(runtime, "ensure_connected", connected)


def make_client(title="@foobar", rows=(), real_username=None, real_cached=False, resolves=True):
    """A never-connected client; `_call` answers ResolveUsernameRequest from memory."""
    client = TelegramClient(StringSession(), 12345, "dummy_hash")
    impostor = _channel(IMPOSTOR_ID, title)
    real = _channel(REAL_ID, "Real chat", username=real_username, hash_=9)
    entities = [impostor, *rows]
    if real_cached:
        entities.append(real)
    client.session.process_entities(SimpleNamespace(users=[], chats=entities))
    client.requests = []

    async def call(sender, request, ordered=False, flood_sleep_threshold=None):
        client.requests.append(request)
        if isinstance(request, functions.contacts.ResolveUsernameRequest) and resolves:
            return types.contacts.ResolvedPeer(
                peer=types.PeerChannel(REAL_ID), chats=[real], users=[]
            )
        raise AssertionError(f"unexpected request {request!r}")

    client._call = call
    client.impostor = impostor
    client.real = real
    return client


def allow(monkeypatch, value="foobar"):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", value)


def peer_id(peer):
    return peer.channel_id


@pytest.mark.asyncio
async def test_collision_resolves_real_username(monkeypatch):
    allow(monkeypatch)
    client = make_client()
    peer = await runtime.resolve_input_entity("@foobar", client)
    assert peer_id(peer) == REAL_ID
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_collision_without_at_sign(monkeypatch):
    allow(monkeypatch)
    client = make_client(title="foobar")
    peer = await runtime.resolve_input_entity("foobar", client)
    assert peer_id(peer) == REAL_ID


@pytest.mark.asyncio
async def test_cached_primary_username_is_free(monkeypatch):
    allow(monkeypatch)
    client = make_client(real_username="foobar", real_cached=True)
    peer = await runtime.resolve_input_entity("@foobar", client)
    assert peer_id(peer) == REAL_ID
    assert client.requests == []


@pytest.mark.asyncio
async def test_secondary_username_resolves_remotely(monkeypatch):
    allow(monkeypatch)
    client = make_client(real_username=None, real_cached=True)
    peer = await runtime.resolve_input_entity("@foobar", client)
    assert peer_id(peer) == REAL_ID
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_unknown_username_is_not_found_by_name(monkeypatch):
    allow(monkeypatch)
    client = make_client(resolves=False)
    with pytest.raises(Exception):
        await runtime.resolve_input_entity("@foobar", client)


@pytest.mark.asyncio
async def test_no_allowlist_is_unchanged(monkeypatch):
    client = make_client()
    peer = await runtime.resolve_input_entity("@foobar", client)
    assert peer_id(peer) == IMPOSTOR_ID
    assert client.requests == []


@pytest.mark.asyncio
async def test_ids_and_me_unchanged_under_allowlist(monkeypatch):
    allow(monkeypatch)
    client = make_client(real_cached=True)
    peer = await runtime.resolve_input_entity(-1000000000000 - REAL_ID, client)
    assert peer_id(peer) == REAL_ID
    assert client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("handle", ["abcde_", "1abcde", "ab__cde", "a" * 40])
@pytest.mark.parametrize("resolver", ["resolve_entity", "resolve_input_entity"])
async def test_malformed_handle_refused_under_allowlist(monkeypatch, handle, resolver):
    allow(monkeypatch, handle)
    client = make_client(rows=[_channel(3003, handle, hash_=5)])
    with pytest.raises(ValueError, match="not a username or an id"):
        await getattr(runtime, resolver)(handle, client)
    assert client.requests == []


@pytest.mark.asyncio
async def test_malformed_handle_unchanged_without_allowlist():
    client = make_client(rows=[_channel(3003, "abcde_", hash_=5)])
    peer = await runtime.resolve_input_entity("abcde_", client)
    assert peer_id(peer) == 3003


@pytest.mark.asyncio
async def test_phone_and_self_not_refused(monkeypatch):
    allow(monkeypatch)
    assert runtime._is_malformed_handle("+15551234567") is False
    assert runtime._is_malformed_handle("me") is False
    assert runtime._is_malformed_handle("12345678") is False
    assert runtime._is_malformed_handle("@validname") is False
    assert runtime._is_malformed_handle("abcde_") is True
