"""Tools that handed a raw string to Telethon use the guarded peer (task 0004)."""

from types import SimpleNamespace

import pytest
from telethon import functions

from telegram_mcp.tools import groups, messages
from test_input_entity_guard import (
    REAL_ID,
    _clean,
    allow,
    make_client,
    peer_id,
)  # noqa: F401


def tool_client(monkeypatch, module, client):
    monkeypatch.setattr(module, "get_client", lambda account=None: client)

    async def connected(cl):
        return None

    monkeypatch.setattr(module, "ensure_connected", connected)


@pytest.mark.asyncio
async def test_get_participants_uses_the_guarded_peer(monkeypatch):
    allow(monkeypatch)
    client = make_client()
    tool_client(monkeypatch, groups, client)
    seen = []

    def iter_participants(target, *, limit):
        seen.append(target)

        async def nothing():
            return
            yield

        return nothing()

    client.iter_participants = iter_participants
    await groups.get_participants("@foobar")
    assert [peer_id(p) for p in seen] == [REAL_ID]


@pytest.mark.asyncio
async def test_get_participants_without_allowlist_resolves_like_telethon(monkeypatch):
    client = make_client()
    tool_client(monkeypatch, groups, client)
    seen = []

    def iter_participants(target, *, limit):
        seen.append(target)

        async def nothing():
            return
            yield

        return nothing()

    client.iter_participants = iter_participants
    await groups.get_participants("@foobar")
    # No list: the plain Telethon lookup (here the cached chat of that name), still an
    # InputPeer rather than the raw string.
    assert [peer_id(p) for p in seen] == [client.impostor.id]
    assert client.requests == []


@pytest.mark.asyncio
async def test_get_recent_actions_uses_the_guarded_peer(monkeypatch):
    allow(monkeypatch)
    client = make_client()
    tool_client(monkeypatch, groups, client)
    sent = []
    resolve = client._call

    async def call(sender, request, **kwargs):
        if isinstance(request, functions.channels.GetAdminLogRequest):
            sent.append(request.channel)
            return SimpleNamespace(events=[], users=[], chats=[])
        return await resolve(sender, request, **kwargs)

    client._call = call
    await groups.get_recent_actions("@foobar")
    assert sent and sent[0].channel_id == REAL_ID


@pytest.mark.asyncio
async def test_send_typing_uses_the_checked_entity(monkeypatch):
    allow(monkeypatch)
    client = make_client()
    tool_client(monkeypatch, messages, client)
    peers = []

    async def call(sender, request, **kwargs):
        if isinstance(request, functions.messages.SetTypingRequest):
            peers.append(request.peer)
            return True
        return await resolve(sender, request, **kwargs)

    resolve = client._call
    client._call = call
    await messages.send_typing("@foobar", 0.5)
    assert peers and all(p.channel_id == REAL_ID for p in peers)
