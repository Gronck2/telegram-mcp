"""Adversarial cases for validate_id phone/number handling (task 0006, tester pass)."""

from types import SimpleNamespace

import pytest
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession

from telegram_mcp import runtime
from telegram_mcp.tools import contacts, messages

PHONE = "+15551234567"
DIGITS_ID = 15551234567
OWNER = types.User(id=555, first_name="Owner", phone="15551234567", access_hash=3)
ID_PEER = types.User(id=DIGITS_ID, first_name="IdPeer", access_hash=5)
CHAN = types.Channel(
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

    for module in (runtime, messages, contacts):
        monkeypatch.setattr(module, "ensure_connected", connected)


def make_client(contact=None):
    client = TelegramClient(StringSession(), 12345, "dummy_hash")
    client.session.process_entities(SimpleNamespace(users=[ID_PEER], chats=[CHAN]))
    client.requests = []

    async def call(sender, request, ordered=False, flood_sleep_threshold=None):
        client.requests.append(request)
        if isinstance(request, functions.contacts.GetContactsRequest):
            users = [contact] if contact else []
            return types.contacts.Contacts(contacts=[], saved_count=0, users=users)
        if isinstance(request, functions.messages.SendMessageRequest):
            return types.Updates(updates=[], users=[], chats=[], date=None, seq=0)
        if isinstance(request, functions.contacts.BlockRequest):
            return True
        if isinstance(request, functions.messages.GetDialogsRequest):
            return types.messages.Dialogs(dialogs=[], messages=[], chats=[], users=[])
        raise AssertionError(f"unexpected request {request!r}")

    client._call = call
    return client


def use(monkeypatch, client):
    for module in (runtime, messages, contacts):
        monkeypatch.setattr(module, "get_client", lambda account=None: client)


def peers_in(requests):
    out = []
    for r in requests:
        for attr in ("peer", "id"):
            p = getattr(r, attr, None)
            if p is not None:
                out.append(p)
    return out


def touches_digit_id(client):
    for p in peers_in(client.requests):
        if getattr(p, "user_id", None) == DIGITS_ID or getattr(p, "channel_id", None) == DIGITS_ID:
            return True
    return False


def body_tool(**options):
    calls = []

    async def body(chat_id, account=None):
        calls.append(chat_id)
        return "ran"

    return runtime.validate_id("chat_id", **options)(body), calls


PHONE_SPELLINGS = [
    "+15551234567",
    " +15551234567 ",
    "+1 555 123 4567",
    "+1-555-123-4567",
    "+(1)5551234567",
    "+٣٣٣",
    "+0",
    "+" + "9" * 60,
]


@pytest.mark.asyncio
@pytest.mark.parametrize("given", PHONE_SPELLINGS)
@pytest.mark.parametrize("param", ["scalar", "list"])
async def test_phone_spellings_reach_body_untouched(given, param):
    fn, calls = body_tool()
    value = given if param == "scalar" else [given, 7, "-100"]
    assert await fn(chat_id=value) == "ran"
    got = calls[0]
    if param == "scalar":
        assert got == given and isinstance(got, str)
    else:
        assert got == [given, 7, -100]


@pytest.mark.asyncio
@pytest.mark.parametrize("given", ["+1_555", "1_000", "--5", "+-5", "٣٣٣x"])
async def test_non_phone_non_number_never_becomes_int(given):
    fn, calls = body_tool()
    await fn(chat_id=given)
    assert not any(isinstance(c, int) for c in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "given, expected",
    [
        ("-1001234567890", -1001234567890),
        ("0", 0),
        ("-0", 0),
        (" 42 ", 42),
        ("9" * 18, 10**18 - 1),
    ],
)
async def test_plain_numbers_still_ints(given, expected):
    fn, calls = body_tool()
    await fn(chat_id=given)
    assert calls == [expected] and type(calls[0]) is int


@pytest.mark.asyncio
@pytest.mark.parametrize("given", ["9" * 30, "-" + "9" * 30])
async def test_long_digit_strings_keep_range_error(given):
    fn, calls = body_tool()
    out = await fn(chat_id=given)
    assert calls == [] and "out of the valid integer range" in out


@pytest.mark.asyncio
async def test_list_error_in_one_item_rejects_whole_list():
    fn, calls = body_tool()
    out = await fn(chat_id=[PHONE, "9" * 30])
    assert calls == [] and "out of the valid integer range" in out


@pytest.mark.asyncio
async def test_alias_int_path_and_ask_payload_unchanged(monkeypatch):
    fn, calls = body_tool()
    monkeypatch.setattr(runtime, "apply_alias", lambda v: 4242 if v == "boss man" else v)
    await fn(chat_id="boss man")
    assert calls == [4242] and isinstance(calls[0], runtime.AliasID)
    calls.clear()
    out = await fn(chat_id="Some Unknown Person")
    assert calls == [] and out != "ran"


def _modes():
    return [
        ("none", None),
        ("digit_id_only", str(DIGITS_ID)),
        ("phone_entry", f"{PHONE},{OWNER.id}"),
        ("contact_id", str(OWNER.id)),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, allow", _modes())
@pytest.mark.parametrize("spelling", ["+15551234567", " +15551234567 ", "+1 555 123 4567"])
async def test_send_message_phone_matrix(monkeypatch, mode, allow, spelling):
    if allow:
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", allow)
    client = make_client(contact=OWNER)
    use(monkeypatch, client)
    out = await messages.send_message(chat_id=spelling, message="hi")
    sends = [r for r in client.requests if isinstance(r, functions.messages.SendMessageRequest)]
    assert not touches_digit_id(client)
    assert not [r for r in client.requests if isinstance(r, functions.messages.GetDialogsRequest)]
    if mode == "digit_id_only":
        assert sends == []
        assert out != "ran"
    elif mode in ("phone_entry", "contact_id", "none"):
        assert len(sends) == 1
        assert sends[0].peer.user_id == OWNER.id
    if allow:
        contact_calls = [
            r for r in client.requests if isinstance(r, functions.contacts.GetContactsRequest)
        ]
        assert contact_calls  # resolved through _phone_lookup, not the id/name columns


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, allow", _modes())
async def test_get_messages_phone_never_reads_digit_peer(monkeypatch, mode, allow):
    if allow:
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", allow)
    client = make_client(contact=OWNER)
    use(monkeypatch, client)
    out = await messages.get_messages(chat_id=PHONE)
    assert not touches_digit_id(client)
    if mode == "digit_id_only":
        assert "ran" not in out and "No messages" not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, allow", _modes())
async def test_block_user_peer_subject_phone(monkeypatch, mode, allow):
    if allow:
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", allow)
    client = make_client(contact=OWNER)
    use(monkeypatch, client)
    out = await contacts.block_user(user_id=PHONE)
    blocks = [r for r in client.requests if isinstance(r, functions.contacts.BlockRequest)]
    assert not touches_digit_id(client)
    if mode == "digit_id_only":
        assert blocks == []
        assert "blocked" not in out
    else:
        assert len(blocks) == 1
        assert blocks[0].id.id == OWNER.id


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, allow", _modes())
async def test_raise_errors_variant_raises_for_denied_phone(monkeypatch, mode, allow):
    if allow:
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", allow)
    client = make_client(contact=OWNER)
    use(monkeypatch, client)
    fn, calls = body_tool(raise_errors=True, peer_subject=True)
    if mode == "digit_id_only":
        with pytest.raises(Exception):
            await fn(chat_id=PHONE)
        assert calls == []
    else:
        assert await fn(chat_id=PHONE) == "ran"
        assert calls == [PHONE]


@pytest.mark.asyncio
async def test_list_of_ids_and_phones_under_allowlist(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", f"{OWNER.id},{PHONE},-100123")
    client = make_client(contact=OWNER)
    use(monkeypatch, client)
    fn, calls = body_tool(peer_subject=True)
    assert await fn(chat_id=[PHONE, "555", "-100123"]) == "ran"
    assert calls == [[PHONE, 555, -100123]]
    calls.clear()
    out = await fn(chat_id=[PHONE, str(DIGITS_ID)])
    assert calls == [] and out != "ran"
