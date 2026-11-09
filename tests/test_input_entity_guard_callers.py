"""Every resolve_input_entity tool caller acts on the real peer, not a title impostor (0004).

The cached chat titled "@foobar" must not receive a request meant for the allowlisted
username @foobar; the tool either sends the request to the real peer or refuses.
"""

from types import SimpleNamespace

import pytest
from telethon import functions, types

from telegram_mcp import runtime
from telegram_mcp.tools import chats, folders, messages
from test_input_entity_guard import (  # noqa: F401
    IMPOSTOR_ID,
    REAL_ID,
    _clean,
    allow,
    make_client,
)


class _Stop(Exception):
    pass


def _folder():
    return types.DialogFilter(
        id=2,
        title=types.TextWithEntities("work", []),
        pinned_peers=[],
        include_peers=[],
        exclude_peers=[],
    )


def harness(monkeypatch, module, client):
    """Record every non-lookup request; answer folder listings; stop at the first write."""
    monkeypatch.setattr(module, "get_client", lambda account=None: client)

    async def connected(cl):
        return None

    monkeypatch.setattr(module, "ensure_connected", connected)
    monkeypatch.setattr(runtime, "get_client", lambda account=None: client)
    monkeypatch.setattr(runtime, "ensure_connected", connected)
    resolve = client._call
    sent = []

    async def call(sender, request, **kwargs):
        if isinstance(request, functions.contacts.ResolveUsernameRequest):
            return await resolve(sender, request, **kwargs)
        sent.append(str(request.to_dict()))
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            return SimpleNamespace(filters=[_folder()])
        raise _Stop()

    client._call = call
    return sent


def assert_real_peer_only(sent):
    joined = "\n".join(sent)
    assert f"'channel_id': {IMPOSTOR_ID}" not in joined
    assert any(f"'channel_id': {REAL_ID}" in s for s in sent), sent


CASES = [
    ("get_send_as", messages, lambda: messages.get_send_as("@foobar")),
    (
        "set_default_send_as",
        messages,
        lambda: messages.set_default_send_as("@foobar", "@foobar"),
    ),
    (
        "forward_message_send_as",
        messages,
        lambda: messages.forward_message("@foobar", 5, "@foobar", send_as="@foobar"),
    ),
    ("send_reaction", messages, lambda: messages.send_reaction("@foobar", 5, "\U0001f44d")),
    ("remove_reaction", messages, lambda: messages.remove_reaction("@foobar", 5)),
    ("get_message_reactions", messages, lambda: messages.get_message_reactions("@foobar", 5)),
    ("save_draft", messages, lambda: messages.save_draft("@foobar", "hi")),
    ("clear_draft", messages, lambda: messages.clear_draft("@foobar")),
    ("add_chat_to_folder", folders, lambda: folders.add_chat_to_folder(2, "@foobar")),
    ("remove_chat_from_folder", folders, lambda: folders.remove_chat_from_folder(2, "@foobar")),
    ("create_folder", folders, lambda: folders.create_folder("work", chat_ids=["@foobar"])),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,module,call", CASES, ids=[c[0] for c in CASES])
async def test_caller_acts_on_real_peer(monkeypatch, name, module, call):
    allow(monkeypatch)
    client = make_client()
    sent = harness(monkeypatch, module, client)
    await call()
    if name == "remove_chat_from_folder":
        # Nothing to remove, so no write request: the lookup itself must have asked
        # Telegram for the real username instead of reading the cached title.
        assert any(
            isinstance(r, functions.contacts.ResolveUsernameRequest) for r in client.requests
        )
        assert f"'channel_id': {IMPOSTOR_ID}" not in "\n".join(sent)
        return
    assert_real_peer_only(sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,module,call", CASES, ids=[c[0] for c in CASES])
async def test_caller_without_allowlist_is_unchanged(monkeypatch, name, module, call):
    """Parity: with no allowlist the legacy lookup still answers from the cache."""
    if name == "forward_message_send_as":
        pytest.skip("resolve_entity (unchanged by this task) already asks the network here")
    client = make_client()
    harness(monkeypatch, module, client)
    await call()
    assert not any(
        isinstance(r, functions.contacts.ResolveUsernameRequest) for r in client.requests
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["mute_chat", "unmute_chat"])
async def test_mute_fallback_acts_on_real_peer(monkeypatch, which):
    allow(monkeypatch)
    client = make_client()
    sent = harness(monkeypatch, chats, client)

    async def broken(*args, **kwargs):
        raise AttributeError("force the raw-API fallback")

    monkeypatch.setattr(chats, "resolve_entity", broken)
    await getattr(chats, which)("@foobar")
    assert_real_peer_only(sent)


@pytest.mark.asyncio
async def test_send_typing_looks_the_username_up_once(monkeypatch):
    """The access check and the request share one lookup (no second, divergent resolve)."""
    allow(monkeypatch)
    client = make_client()
    harness(monkeypatch, messages, client)
    await messages.send_typing("@foobar", 0.5)
    lookups = [
        r for r in client.requests if isinstance(r, functions.contacts.ResolveUsernameRequest)
    ]
    assert len(lookups) == 1
