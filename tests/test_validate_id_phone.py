"""validate_id keeps "+"-led phone strings as phones, not numeric ids (task 0006)."""

from types import SimpleNamespace

import pytest
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession

from telegram_mcp import runtime

PHONE = "+15551234567"
DIGITS_ID = 15551234567
OWNER = types.User(id=555, first_name="Owner", phone="15551234567", access_hash=3)
OTHER = types.User(id=556, first_name="Other", phone="15559999999", access_hash=4)
ID_PEER = types.User(id=DIGITS_ID, first_name="IdPeer", access_hash=5)
IMPOSTOR = types.Channel(
    id=1001, title=PHONE, photo=types.ChatPhotoEmpty(), date=None, access_hash=7, megagroup=True
)
CHANNEL_SAME_NUMBER = types.Channel(
    id=DIGITS_ID, title="Same", photo=types.ChatPhotoEmpty(), date=None, access_hash=9
)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for name in (
        "TELEGRAM_ALLOWED_CHAT_IDS",
        "TELEGRAM_ALLOWED_READ_CHATS",
        "TELEGRAM_ALLOWED_WRITE_CHATS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", None)
    monkeypatch.setenv("TELEGRAM_ALIASES_FILE", str(tmp_path / "aliases.json"))

    async def connected(client=None):
        return None

    monkeypatch.setattr(runtime, "ensure_connected", connected)


def make_client(contact=None):
    client = TelegramClient(StringSession(), 12345, "dummy_hash")
    client.session.process_entities(
        SimpleNamespace(users=[ID_PEER], chats=[IMPOSTOR, CHANNEL_SAME_NUMBER])
    )
    client.requests = []

    async def call(sender, request, ordered=False, flood_sleep_threshold=None):
        client.requests.append(request)
        if isinstance(request, functions.contacts.GetContactsRequest):
            users = [contact] if contact else []
            return types.contacts.Contacts(contacts=[], saved_count=0, users=users)
        raise AssertionError(f"unexpected request {request!r}")

    client._call = call
    return client


def use(monkeypatch, client):
    monkeypatch.setattr(runtime, "get_client", lambda account=None: client)


def tool(name="send_message", **options):
    calls = []

    async def body(chat_id, account=None):
        calls.append(chat_id)
        return "ran"

    body.__name__ = name
    return runtime.validate_id("chat_id", **options)(body), calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "given, expected",
    [
        (PHONE, PHONE),
        (f" {PHONE} ", f" {PHONE} "),
        ("+1555", "+1555"),
        ("+1 555 123 4567", "+1 555 123 4567"),
        ("+1-555-123-4567", "+1-555-123-4567"),
        ("+1 (555) 123-4567", "+1 (555) 123-4567"),
        ("+5", "+5"),
        ("+5_5", "+5_5"),
        ("15551234567", DIGITS_ID),
        (" 15551234567 ", DIGITS_ID),
        ("-100123", -100123),
        ("0", 0),
        (-100123, -100123),
        (DIGITS_ID, DIGITS_ID),
    ],
)
async def test_value_conversion(given, expected):
    fn, calls = tool()
    assert await fn(chat_id=given) == "ran"
    assert calls == [expected]
    assert type(calls[0]) is type(expected)


@pytest.mark.asyncio
async def test_list_items_keep_phones_as_strings():
    fn, calls = tool()
    assert await fn(chat_id=[PHONE, " +1555 ", "15551234567", "-100123"]) == "ran"
    assert calls == [[PHONE, " +1555 ", DIGITS_ID, -100123]]


@pytest.mark.asyncio
@pytest.mark.parametrize("given", ["1_000", "5_5", "--5", "- 5"])
async def test_int_only_quirks_are_not_ids(given):
    fn, calls = tool()
    await fn(chat_id=given)
    # asked the user or left as a string for the resolvers; never an int id
    assert not any(isinstance(c, int) for c in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("given", ["9" * 25, "-" + "9" * 25])
async def test_out_of_range_digits_keep_error(given):
    fn, calls = tool()
    out = await fn(chat_id=given)
    assert calls == []
    assert "out of the valid integer range" in out


@pytest.mark.asyncio
async def test_no_allowlist_phone_resolves_by_phone_not_id(monkeypatch):
    client = make_client(contact=OWNER)
    use(monkeypatch, client)
    fn, calls = tool()
    assert await fn(chat_id=PHONE) == "ran"
    [chat_id] = calls
    assert chat_id == PHONE
    entity = await runtime.resolve_entity(chat_id, client)
    assert entity.id == OWNER.id


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["plain", "peer_subject", "raise_errors"])
async def test_allowlist_of_the_digit_id_does_not_grant_the_phone(monkeypatch, variant):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", str(DIGITS_ID))
    client = make_client(contact=OTHER)
    use(monkeypatch, client)
    options = {
        "plain": {},
        "peer_subject": {"peer_subject": True},
        "raise_errors": {"raise_errors": True},
    }[variant]
    fn, calls = tool(**options)
    try:
        out = await fn(chat_id=PHONE)
    except Exception as exc:  # raise_errors variants may raise instead of returning
        out = str(exc)
    assert calls == []
    assert out != "ran"
    # the id-peer / same-numbered channel (and marked variants) was never queried
    allowed = (functions.contacts.GetContactsRequest, functions.messages.GetDialogsRequest)
    assert all(isinstance(r, allowed) for r in client.requests)


@pytest.mark.asyncio
async def test_allowlist_with_the_phone_entry_allows_via_phone_lookup(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", f"{PHONE},{OWNER.id}")
    client = make_client(contact=OWNER)
    use(monkeypatch, client)
    fn, calls = tool()
    assert await fn(chat_id=PHONE) == "ran"
    assert calls == [PHONE]
    entity = await runtime.resolve_entity(PHONE, client)
    assert entity.id == OWNER.id
    assert all(isinstance(r, functions.contacts.GetContactsRequest) for r in client.requests)


@pytest.mark.asyncio
async def test_allowlist_with_resolved_users_id_allows_the_phone(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", str(OWNER.id))
    use(monkeypatch, make_client(contact=OWNER))
    fn, calls = tool()
    assert await fn(chat_id=PHONE) == "ran"
    assert calls == [PHONE]


@pytest.mark.asyncio
async def test_allowlist_phone_entry_does_not_grant_the_digit_string(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", f"{PHONE}")
    use(monkeypatch, make_client(contact=OWNER))
    fn, calls = tool()
    out = await fn(chat_id="15551234567")
    assert calls == []
    assert out != "ran"


@pytest.mark.asyncio
async def test_unknown_phone_under_allowlist_is_denied_not_name_matched(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", f"{DIGITS_ID},{IMPOSTOR.id}")
    client = make_client()
    use(monkeypatch, client)
    fn, calls = tool()
    out = await fn(chat_id=PHONE)
    assert calls == []
    assert out != "ran"
