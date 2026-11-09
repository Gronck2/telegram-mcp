"""Strings the allowlist grants by string are that username or refused (task 0004)."""

import pytest
from telethon import functions, utils

from telegram_mcp import runtime
from telegram_mcp.tools import folders, messages
from test_input_entity_guard import (  # noqa: F401
    IMPOSTOR_ID,
    REAL_ID,
    _clean,
    allow,
    make_client,
)
from test_input_entity_guard_callers import harness

RESOLVERS = ("resolve_entity", "resolve_input_entity")
BAD = ["@@abcd", "@ abcd", "@\tabcd", "@@ abcd", "@@ABCD", " @@abcd "]


@pytest.mark.parametrize("ref", BAD)
def test_allowlist_normalised_username_forms_refused(ref):
    assert runtime._is_malformed_handle(ref) is True


@pytest.mark.parametrize("ref", ["@abcd", "abcd", "ABCD", " @abcd", "t.me/abcd", "@abc", "abc"])
def test_valid_short_handles_not_refused(ref):
    assert utils.parse_username(ref)[0]
    assert runtime._is_malformed_handle(ref) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver", RESOLVERS)
@pytest.mark.parametrize("entry,ref", [("abcd", b) for b in BAD] + [("abc", "@@abc")])
async def test_refused_with_zero_requests(monkeypatch, resolver, entry, ref):
    allow(monkeypatch, "@" + entry)
    # Not granted by string either: an alias could shadow it (apply_alias strips "@").
    assert not runtime.is_chat_allowed(ref)
    client = make_client(title=ref)
    with pytest.raises((ValueError, runtime.AliasNeedsUser)):
        await getattr(runtime, resolver)(ref, client)
    assert client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver", RESOLVERS)
@pytest.mark.parametrize("entry,ref", [("abcd", "@abcd"), ("abcd", "abcd"), ("abc", "@abc")])
async def test_valid_short_handles_resolve_by_username(monkeypatch, resolver, entry, ref):
    allow(monkeypatch, "@" + entry)
    client = make_client(title=ref)
    await getattr(runtime, resolver)(ref, client)
    assert any(isinstance(r, functions.contacts.ResolveUsernameRequest) for r in client.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver", RESOLVERS)
@pytest.mark.parametrize("ref", BAD)
async def test_no_allowlist_parity(monkeypatch, resolver, ref):
    client = make_client(title=ref)
    try:
        await getattr(runtime, resolver)(ref, client)
    except Exception as error:  # the refusal must not be the allowlist one
        assert "allowlist" not in str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", BAD)
async def test_create_folder_and_export_never_use_impostor(monkeypatch, tmp_path, ref):
    allow(monkeypatch, "@abcd")
    for module, call in (
        (folders, lambda: folders.create_folder("work", chat_ids=[ref])),
        (
            messages,
            lambda: messages.export_chat_history(ref, output_path=str(tmp_path / "o.jsonl")),
        ),
    ):
        client = make_client(title=ref)
        sent = harness(monkeypatch, module, client)
        monkeypatch.setattr(module, "get_client", lambda account=None, c=client: c)
        await call()
        assert f"'channel_id': {IMPOSTOR_ID}" not in "\n".join(sent)
        assert not any(
            isinstance(r, functions.contacts.ResolveUsernameRequest) for r in client.requests
        )
