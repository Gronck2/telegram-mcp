"""The allowlist grant and the username refusal cannot drift apart (task 0004).

For generated strings with an allowlist entry for their cleaned username: whenever the
allowlist grants the string, either Telethon would look up exactly that username (so a
ResolveUsernameRequest is used) or both resolvers refuse. A cached display name is never
the answer.
"""

import itertools
import json

import pytest
from telethon import functions, utils

from telegram_mcp import runtime
from telegram_mcp.tools import contacts, messages
from test_input_entity_guard import (  # noqa: F401
    IMPOSTOR_ID,
    REAL_ID,
    _clean,
    allow,
    make_client,
)
from test_input_entity_guard_callers import harness

RESOLVERS = ("resolve_entity", "resolve_input_entity")
NAMES = ["abc", "abcd", "abcde", "a" * 32, "a" * 33, "ab_cd", "abcde_", "ab__cd", "1abcd", "AbCd"]
PREFIXES = ["", "@", "@@", " @", "@ ", "\t@", "@\t", "\n@", "@\n", "  ", "t.me/", "@t.me/"]
SUFFIXES = ["", " ", "\t", "\n", "/", "@"]
LOOKALIKES = ["＠abcd", "Kabcd", "abcс", "аbcd", "@аbcd", "​@abcd"]


def _strings():
    out = {p + n + s for p, n, s in itertools.product(PREFIXES, NAMES, SUFFIXES)}
    out.update(LOOKALIKES)
    out.update(
        {"https://t.me/abcd", "http://telegram.me/abcd", "tg://resolve?domain=abcd", "@ abcd"}
    )
    return sorted(out)


def _cleaned(s):
    return s.strip().lstrip("@").strip().lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver", RESOLVERS)
async def test_granted_string_is_that_username_or_refused(monkeypatch, resolver):
    checked = granted_refused = granted_looked_up = 0
    for s in _strings():
        cleaned = _cleaned(s)
        if not cleaned or "\x00" in s:
            continue
        cname, cinvite = utils.parse_username(cleaned)
        if cname != cleaned or cinvite:
            continue  # the entry is not a username, so nothing here is a username grant
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "@" + cleaned)
        if not runtime.is_chat_allowed(s):
            continue
        checked += 1
        client = make_client(title=s)
        try:
            peer = await getattr(runtime, resolver)(s, client)
        except Exception:
            peer = None
        name, is_invite = utils.parse_username(s)
        looked_up = (
            bool(name) and not is_invite and s.lower() not in runtime._SELF_REFS
        ) and name.lower() == cleaned
        asked = [
            r
            for r in client.requests
            if not isinstance(r, functions.contacts.ResolveUsernameRequest)
        ]
        assert asked == [], s
        if peer is not None:
            # An answer must come from the username lookup, never the impostor title.
            assert utils.get_peer_id(peer) != -1000000000000 - IMPOSTOR_ID, repr(s)
            assert getattr(peer, "channel_id", None) != IMPOSTOR_ID, repr(s)
            assert looked_up or client.requests == [], repr(s)
        if looked_up:
            granted_looked_up += 1
        else:
            granted_refused += 1
            assert peer is None, repr(s)
            assert client.requests == [], repr(s)
    assert checked > 100 and granted_refused > 20 and granted_looked_up > 20


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver", RESOLVERS)
async def test_ungranted_strings_never_reach_impostor(monkeypatch, resolver):
    """Even without a matching entry the display-name column is never the answer."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "zzzzz")
    for s in _strings():
        if "\x00" in s:
            continue
        client = make_client(title=s)
        try:
            peer = await getattr(runtime, resolver)(s, client)
        except Exception:
            continue
        assert getattr(peer, "channel_id", None) != IMPOSTOR_ID, repr(s)


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["@@abcd", "@@ABCD", " @@abcd "])
async def test_export_unread_messages_never_reaches_impostor(monkeypatch, tmp_path, ref):
    allow(monkeypatch, "@abcd")
    client = make_client(title=ref)
    sent = harness(monkeypatch, messages, client)
    out = tmp_path / "o.json"
    monkeypatch.setattr(messages, "_resolve_writable_file_path", _path_ok(out))
    await messages.export_unread_messages([ref], output_path=str(out))
    assert f"'channel_id': {IMPOSTOR_ID}" not in "\n".join(sent)
    assert client.requests == []
    if out.exists():
        assert str(IMPOSTOR_ID) not in json.dumps(json.loads(out.read_text() or "{}"))


def _path_ok(out):
    async def resolve(**kwargs):
        return out, None

    return resolve


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["@@abcd", "@ abcd", "@@ABCD"])
async def test_set_contact_alias_never_reaches_impostor(monkeypatch, ref):
    allow(monkeypatch, "@abcd")
    client = make_client(title=ref)
    harness(monkeypatch, contacts, client)
    result = await contacts.set_contact_alias("nick name", ref)
    assert client.requests == []
    assert str(IMPOSTOR_ID) not in result
    assert '"saved": true' not in result.lower()


@pytest.mark.asyncio
async def test_set_contact_alias_valid_username_still_resolves(monkeypatch):
    allow(monkeypatch, "@abcde")
    client = make_client(title="@abcde")
    harness(monkeypatch, contacts, client)
    result = await contacts.set_contact_alias("nick name", "@abcde")
    assert any(isinstance(r, functions.contacts.ResolveUsernameRequest) for r in client.requests)
    assert str(IMPOSTOR_ID) not in result


@pytest.mark.asyncio
async def test_export_unread_valid_short_username_still_works(monkeypatch, tmp_path):
    allow(monkeypatch, "@abcd")
    client = make_client(title="@abcd")
    harness(monkeypatch, messages, client)
    out = tmp_path / "o.json"
    monkeypatch.setattr(messages, "_resolve_writable_file_path", _path_ok(out))
    await messages.export_unread_messages(["@abcd"], output_path=str(out))
    assert any(isinstance(r, functions.contacts.ResolveUsernameRequest) for r in client.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver", RESOLVERS)
async def test_no_allowlist_parity_generated(monkeypatch, resolver):
    """No allowlist: nothing is refused by the guard and display names still answer."""
    for s in _strings():
        if "\x00" in s:
            continue
        client = make_client(title=s)
        try:
            await getattr(runtime, resolver)(s, client)
        except Exception as error:
            assert "allowlist" not in str(error), repr(s)
