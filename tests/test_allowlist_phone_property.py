"""Phone allowlist entries and the resolvers cannot drift apart (task 0005).

For generated phone entries and caller strings: whenever the allowlist grants a caller by
string, Telethon must read that caller as a phone with the same digits as the entry and
resolve it through the phone row / GetContacts, never through the display-name column.
Callers that merely look like the phone (``@555-123-4567``) are denied by string.
"""

import itertools

import pytest
from telethon import functions, types, utils

from telegram_mcp import runtime
from test_allowlist_inert_entries_extra import _phone_client
from test_input_entity_guard import IMPOSTOR_ID, _clean, allow  # noqa: F401

RESOLVERS = ("resolve_entity", "resolve_input_entity")
ENTRIES = ["555-123-4567", "8 (999) 123-45-67", "+7 999 123-45-67", "79991234567"]
PREFIXES = ["", "@", "@@", "+", " ", "\t", " @", "@ ", "+@", "@+", "t.me/", "t.me/+"]
BODIES = [
    "555-123-4567",
    "5551234567",
    "555 123 4567",
    "(555) 123-4567",
    "8 (999) 123-45-67",
    "89991234567",
    "7 999 123-45-67",
    "79991234567",
    "7999 123 45 67",
    "５５５１２３４５６７",  # full-width digits
    "٥٥٥١٢٣٤٥٦٧",  # arabic-indic digits
    "555-123-4567x",
    "5551234568",
]
SUFFIXES = ["", " ", "\n", "-", "/", "@"]


def _callers():
    out = {p + b + s for p, b, s in itertools.product(PREFIXES, BODIES, SUFFIXES)}
    out.update({"+", "@+", "++79991234567", "+-79991234567", "tel:+79991234567"})
    return sorted(out)


def _entry_digits(entry):
    return "".join(c for c in entry if c.isdigit())


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver", RESOLVERS)
@pytest.mark.parametrize("entry", ENTRIES)
async def test_granted_caller_is_that_phone_or_refused(monkeypatch, entry, resolver):
    allow(monkeypatch, entry)
    digits = _entry_digits(entry)
    granted = denied = 0
    for caller in _callers():
        if not runtime.is_chat_allowed(caller):
            denied += 1
            continue
        granted += 1
        # What the string check grants is exactly a phone Telethon reads the same way.
        assert utils.parse_phone(caller) == digits, repr(caller)
        client = _phone_client(title=caller, phone_row=True)
        try:
            peer = await getattr(runtime, resolver)(caller, client)
        except Exception:
            peer = None
        if peer is not None:
            # The answer is the phone-row user, never the same-titled impostor chat.
            assert getattr(peer, "channel_id", None) != IMPOSTOR_ID, repr(caller)
            assert getattr(peer, "chat_id", None) != IMPOSTOR_ID, repr(caller)
            assert getattr(peer, "user_id", getattr(peer, "id", None)) == 77, repr(caller)
        # No username lookup is ever made for a phone caller.
        assert not any(
            isinstance(r, functions.contacts.ResolveUsernameRequest) for r in client.requests
        ), repr(caller)
    assert granted >= 5 and denied > 100


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver", RESOLVERS)
@pytest.mark.parametrize("entry", ENTRIES)
async def test_denied_phone_lookalikes_never_reach_name_column(monkeypatch, entry, resolver):
    """Strings only a cached title could answer are refused by both resolvers."""
    allow(monkeypatch, entry)
    for caller in _callers():
        if runtime.is_chat_allowed(caller):
            continue
        cleaned = caller.strip().lstrip("@").strip()
        if not utils.parse_phone(cleaned) or utils.parse_phone(caller):
            continue  # only phones once "@" is stripped are the look-alikes
        client = _phone_client(title=caller)
        try:
            peer = await getattr(runtime, resolver)(caller, client)
        except Exception:
            continue
        assert getattr(peer, "channel_id", None) != IMPOSTOR_ID, repr(caller)
        assert getattr(peer, "chat_id", None) != IMPOSTOR_ID, repr(caller)


@pytest.mark.parametrize("entry", ENTRIES)
def test_phone_entry_never_matches_by_string_without_phone_parse(monkeypatch, entry):
    """A handle-shaped string never grants a phone entry (usernames cannot start with +)."""
    allow(monkeypatch, entry)
    digits = _entry_digits(entry)
    for caller in ("@" + entry, "@+" + digits, "+" + digits + "@", "t.me/+" + digits):
        assert utils.parse_phone(caller) != digits or not caller.startswith("@")
        if not utils.parse_phone(caller):
            assert not runtime.is_chat_allowed(caller), repr(caller)


def test_plus_prefixed_digits_entry_is_a_phone_not_an_id(monkeypatch):
    """ "+123" stays a phone entry (as on the base branch): it never grants the id 123."""
    allow(monkeypatch, "+123")
    assert runtime._parse_allowed_chat_ids("+123") == {"+123"}
    assert runtime.is_chat_allowed("+123")
    assert runtime.is_chat_allowed(" +123 ")
    assert not runtime.is_chat_allowed("123")
    assert not runtime.is_chat_allowed(123)
    assert not runtime.is_chat_allowed("@+123")
    allow(monkeypatch, "123")
    assert runtime.is_chat_allowed(123) and runtime.is_chat_allowed("123")
    assert not runtime.is_chat_allowed("+123")


def test_secondary_usernames_still_match_active_only(monkeypatch):
    allow(monkeypatch, "@second_name")
    active = types.User(
        id=5,
        usernames=[types.Username(username="Second_Name", editable=False, active=True)],
    )
    inactive = types.User(
        id=6,
        usernames=[types.Username(username="second_name", editable=False, active=False)],
    )
    assert runtime.is_chat_allowed(5, entity=active)
    assert not runtime.is_chat_allowed(6, entity=inactive)
    assert not runtime.is_chat_allowed(7, entity=types.User(id=7))
