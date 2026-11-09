"""Further checks for inert allowlist entries (task 0005): phones, t.me forms, warnings."""

import datetime
import re
from types import SimpleNamespace

import pytest
from telethon import functions, types

from telegram_mcp import runtime
from test_input_entity_guard import IMPOSTOR_ID, allow, make_client  # noqa: F401

PHONE = "+7999123"
NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def _phone_client(title, phone_row=False):
    """Client holding a same-titled impostor; answers GetContacts with no match."""
    client = make_client(title=title)
    if phone_row:
        user = types.User(id=77, access_hash=5, phone="7999123", first_name="Real")
        client.session.process_entities(SimpleNamespace(users=[user], chats=[]))
    inner = client._call
    real_user = types.User(id=77, access_hash=5, phone="7999123", first_name="Real")

    async def call(sender, request, ordered=False, flood_sleep_threshold=None):
        if isinstance(request, functions.contacts.GetContactsRequest):
            client.requests.append(request)
            return types.contacts.Contacts(
                contacts=[], saved_count=0, users=[real_user] if phone_row else []
            )
        if isinstance(request, functions.updates.GetStateRequest):
            return types.updates.State(pts=1, qts=0, date=NOW, seq=0, unread_count=0)
        if isinstance(request, functions.updates.GetDifferenceRequest):
            return types.updates.DifferenceEmpty(date=NOW, seq=0)
        if isinstance(request, functions.users.GetUsersRequest):
            client.requests.append(request)
            return [real_user]
        return await inner(sender, request, ordered, flood_sleep_threshold)

    client._call = call
    return client


def test_phone_entry_is_kept_and_matches_phone_callers(monkeypatch):
    allow(monkeypatch, PHONE)
    assert runtime._parse_allowed_chat_ids(PHONE) == {PHONE}
    assert runtime.is_chat_allowed(PHONE)
    assert not runtime.is_chat_allowed("+7999124")
    assert not runtime.is_chat_allowed("my group")


@pytest.mark.asyncio
async def test_phone_caller_resolves_by_phone_row_not_name(monkeypatch):
    allow(monkeypatch, PHONE)
    client = _phone_client(title=PHONE, phone_row=True)
    peer = await runtime.resolve_input_entity(PHONE, client)
    assert isinstance(peer, types.InputPeerUser) and peer.user_id == 77
    # Only Telethon's own get-me bookkeeping; no lookup of the phone itself.
    assert [r.to_dict()["id"] for r in client.requests] == [[{"_": "InputUserSelf"}]]
    entity = await runtime.resolve_entity(PHONE, client)
    assert entity.id == 77  # never the impostor titled like the phone


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver", ["resolve_entity", "resolve_input_entity"])
async def test_phone_miss_never_returns_title_impostor(monkeypatch, resolver):
    allow(monkeypatch, PHONE)
    client = _phone_client(title=PHONE)
    with pytest.raises(ValueError):
        await getattr(runtime, resolver)(PHONE, client)
    assert any(isinstance(r, functions.contacts.GetContactsRequest) for r in client.requests)


@pytest.mark.parametrize("entry", ["t.me/Some_Name", "https://t.me/Some_Name", "@T.me/Some_Name"])
def test_tme_entry_matches_bare_and_at_callers(monkeypatch, entry):
    allow(monkeypatch, entry)
    assert runtime._parse_allowed_chat_ids(entry) == {"some_name"}
    for caller in ("some_name", "@Some_Name"):
        assert runtime.is_chat_allowed(caller), caller
    assert not runtime.is_chat_allowed("other_name")


@pytest.mark.parametrize(
    "var",
    [
        "TELEGRAM_ALLOWED_CHAT_IDS",
        "TELEGRAM_ALLOWED_READ_CHATS",
        "TELEGRAM_ALLOWED_WRITE_CHATS",
    ],
)
def test_mixed_entries_in_each_variable(monkeypatch, var):
    monkeypatch.setenv(var, "my group, 42, @Good_Name, t.me/+hash")
    assert runtime._parse_allowed_chat_ids("my group, 42, @Good_Name, t.me/+hash") == {
        runtime._INERT_CHAT_ENTRY,
        42,
        "good_name",
    }
    access = "write" if var.endswith("WRITE_CHATS") else "read"
    assert runtime.is_chat_allowed(42, access=access)
    assert runtime.is_chat_allowed("good_name", access=access)
    assert not runtime.is_chat_allowed("my group", access=access)
    assert not runtime.is_chat_allowed("t.me/+hash", access=access)
    assert not runtime.is_chat_allowed(43, access=access)


def test_warning_has_no_raw_control_characters_and_is_once_per_process(monkeypatch, capsys):
    monkeypatch.setattr(runtime, "_WARNED_IGNORED_ENTRIES", set())
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "bad\x1b[31m\rtext  group, 5")
    runtime.warn_ignored_chat_allowlist_entries()
    runtime.warn_ignored_chat_allowlist_entries()
    for _ in range(3):
        runtime.is_chat_allowed("x")
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1
    assert not re.search(r"[\x00-\x09\x0b-\x1f\x7f]", err)
    assert "TELEGRAM_ALLOWED_CHAT_IDS" in err


@pytest.mark.asyncio
async def test_inert_entry_caller_string_is_denied_before_resolution(monkeypatch):
    """The resolvers do not decide access; the string check must refuse the caller first."""
    allow(monkeypatch, "my group")
    client = make_client(title="my group")
    assert not runtime.is_chat_allowed("my group")
    assert not runtime.is_chat_allowed(IMPOSTOR_ID)
    assert not runtime.is_chat_allowed(-1000000000000 - IMPOSTOR_ID)
    assert client.requests == []
