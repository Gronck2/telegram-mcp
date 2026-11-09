"""Further checks for inert allowlist entries (task 0005): phones, t.me forms, warnings."""

import datetime
import re
from types import SimpleNamespace

import pytest
from telethon import functions, types

from telegram_mcp import runtime
from test_input_entity_guard import IMPOSTOR_ID, _clean, allow, make_client  # noqa: F401

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
    # Answered from the phone row: no network request at all.
    assert client.requests == []
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


# --- phone entries without "+" and "@"-prefixed look-alikes (code review round 1) ---

LOCAL_PHONES = ["555-123-4567", "8 (999) 123-45-67"]


@pytest.mark.parametrize("entry", LOCAL_PHONES + ["+7 999 123 45 67"])
def test_phone_entry_is_canonical_digits(monkeypatch, entry):
    digits = "".join(c for c in entry if c.isdigit())
    assert runtime._parse_allowed_chat_ids(entry) == {"+" + digits}
    allow(monkeypatch, entry)
    assert runtime.is_chat_allowed(entry)
    assert runtime.is_chat_allowed("+" + digits)


@pytest.mark.parametrize("entry", LOCAL_PHONES)
def test_at_prefixed_phone_lookalike_is_denied_and_malformed(monkeypatch, entry):
    allow(monkeypatch, entry)
    for caller in ("@" + entry, "@+" + entry, "@ " + entry):
        assert not runtime.is_chat_allowed(caller), caller
        assert runtime._is_malformed_handle(caller), caller
    assert not runtime._is_malformed_handle(entry)


def test_phone_caller_variants_match_consistently(monkeypatch):
    allow(monkeypatch, "+79991234567")
    for caller in ("+7 999 123 45 67", "+7(999)123-45-67", " +79991234567 "):
        assert runtime.is_chat_allowed(caller), caller
        assert not runtime._is_malformed_handle(caller), caller
    for caller in ("8 (999) 123-45-67", "@+79991234567", "+79991234568"):
        assert not runtime.is_chat_allowed(caller), caller


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", LOCAL_PHONES)
@pytest.mark.parametrize("resolver", ["resolve_entity", "resolve_input_entity"])
async def test_at_phone_lookalike_never_reaches_name_column(monkeypatch, entry, resolver):
    allow(monkeypatch, entry)
    caller = "@" + entry
    assert not runtime.is_chat_allowed(caller)
    client = make_client(title=caller)
    try:
        result = await getattr(runtime, resolver)(caller, client)
    except Exception:
        result = None
    assert getattr(result, "id", None) != IMPOSTOR_ID
    assert getattr(result, "channel_id", None) != IMPOSTOR_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", LOCAL_PHONES)
async def test_create_folder_at_phone_lookalike(monkeypatch, entry):
    from telegram_mcp.tools import folders
    from test_input_entity_guard_callers import harness

    allow(monkeypatch, entry)
    client = make_client(title="@" + entry)
    sent = harness(monkeypatch, folders, client)
    try:
        await folders.create_folder("work", chat_ids=["@" + entry])
    except Exception:
        pass
    assert f"'channel_id': {IMPOSTOR_ID}" not in "\n".join(sent)
    assert client.requests == []
