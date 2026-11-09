"""Widened handle refusal and numeric conversion keep legitimate refs working (task 0004)."""

import pytest
from telethon import functions, types

from telegram_mcp import runtime
from test_input_entity_guard import (  # noqa: F401
    IMPOSTOR_ID,
    REAL_ID,
    _channel,
    _clean,
    allow,
    make_client,
)
from test_input_entity_guard_refs import _peer_key

NUMERIC_REAL_ID = 1234567890
MARKED = -1001234567890
RESOLVERS = ("resolve_entity", "resolve_input_entity")


def _key(got):
    return getattr(got, "channel_id", None) or getattr(got, "id", None) or _peer_key(got)


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver", RESOLVERS)
@pytest.mark.parametrize(
    "ref",
    ["@foobar", "foobar", "t.me/foobar", "https://t.me/foobar", "@FooBar", "FOOBAR", " foobar "],
)
async def test_valid_usernames_not_refused(monkeypatch, resolver, ref):
    allow(monkeypatch, "foobar")
    client = make_client(title=ref)
    got = await getattr(runtime, resolver)(ref, client)
    assert _key(got) == REAL_ID
    assert any(isinstance(r, functions.contacts.ResolveUsernameRequest) for r in client.requests)


@pytest.mark.parametrize(
    "ref", ["@foobar", "foobar", "t.me/foobar", "@FooBar", "me", "+1 555 123", "12345678"]
)
def test_valid_refs_not_malformed(ref):
    assert runtime._is_malformed_handle(ref) is False


@pytest.mark.parametrize("ref", ["abcde_", "@ foobar", "tg://resolve?domain=foobar", "ab__cde"])
def test_malformed_still_refused(ref):
    assert runtime._is_malformed_handle(ref) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver", RESOLVERS)
@pytest.mark.parametrize("alias", ["лена", "Лена", "my friend", "My  Friend"])
@pytest.mark.parametrize("env", ["TELEGRAM_ALLOWED_CHAT_IDS", "TELEGRAM_ALLOWED_READ_CHATS"])
async def test_saved_alias_resolves_under_allowlist(monkeypatch, resolver, alias, env):
    monkeypatch.setenv(env, str(MARKED))
    runtime.save_aliases({"лена": MARKED, "my friend": MARKED})
    client = make_client(title=alias, rows=[_channel(NUMERIC_REAL_ID, "Real", hash_=5)])

    async def call(sender, request, ordered=False, flood_sleep_threshold=None):
        client.requests.append(request)
        if isinstance(request, functions.channels.GetChannelsRequest):
            return types.messages.Chats(chats=[_channel(NUMERIC_REAL_ID, "Real", hash_=5)])
        raise AssertionError(f"unexpected {request!r}")

    client._call = call
    got = await getattr(runtime, resolver)(alias, client)
    assert _key(got) == NUMERIC_REAL_ID
    assert not any(
        isinstance(r, functions.contacts.ResolveUsernameRequest) for r in client.requests
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver", RESOLVERS)
async def test_alias_to_disallowed_chat_not_used(monkeypatch, resolver):
    allow(monkeypatch, "foobar")
    runtime.save_aliases({"лена": MARKED})
    client = make_client(title="лена", rows=[_channel(NUMERIC_REAL_ID, "Real", hash_=5)])
    with pytest.raises(Exception):
        got = await getattr(runtime, resolver)("лена", client)
        assert _key(got) != IMPOSTOR_ID
        raise AssertionError("alias to a chat outside the allowlist resolved")


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver", RESOLVERS)
async def test_free_text_title_same_result(monkeypatch, resolver):
    allow(monkeypatch, str(MARKED))
    outcomes = []
    for name in RESOLVERS:
        client = make_client(title="My Group", rows=[_channel(NUMERIC_REAL_ID, "Real", hash_=5)])
        try:
            outcomes.append(_key(await getattr(runtime, name)("My Group", client)))
        except Exception as error:
            outcomes.append(type(error).__name__ if not isinstance(error, ValueError) else "V")
    assert outcomes[0] == outcomes[1], outcomes


@pytest.mark.asyncio
async def test_phone_without_row_goes_to_contacts_not_names(monkeypatch):
    allow(monkeypatch, "+1555123")
    client = make_client(title="+1 555 123")

    async def call(sender, request, ordered=False, flood_sleep_threshold=None):
        client.requests.append(request)
        if isinstance(request, functions.contacts.GetContactsRequest):
            return types.contacts.Contacts(contacts=[], saved_count=0, users=[])
        raise AssertionError(f"unexpected {request!r}")

    client._call = call
    with pytest.raises(Exception):
        await runtime.resolve_input_entity("+1 555 123", client)
    assert any(isinstance(r, functions.contacts.GetContactsRequest) for r in client.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver", RESOLVERS)
async def test_no_allowlist_numeric_string_becomes_int(monkeypatch, resolver):
    seen = []

    class Client:
        async def get_entity(self, x):
            seen.append(x)
            return types.InputPeerSelf()

        async def get_input_entity(self, x):
            seen.append(x)
            return types.InputPeerSelf()

    # Task 0006 round 3: digit strings are ids in every mode; as strings Telethon
    # would read them as phones (name column / GetContacts).
    await getattr(runtime, resolver)("12345678", Client())
    assert seen == [12345678]


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver", RESOLVERS)
async def test_allowlist_numeric_string_becomes_int(monkeypatch, resolver):
    allow(monkeypatch, "12345678")
    seen = []

    class Client:
        async def get_entity(self, x):
            seen.append(x)
            return types.InputPeerSelf()

        async def get_input_entity(self, x):
            seen.append(x)
            return types.InputPeerSelf()

    await getattr(runtime, resolver)(" 12345678 ", Client())
    assert seen == [12345678]
