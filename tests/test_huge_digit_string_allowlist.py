"""Task 0006 round 5: a digit string beyond int()'s 4300 digits is a clean error under
an allowlist too (no raw ValueError), and sends no Telegram request."""

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from telegram_mcp import runtime
from telegram_mcp.tools import folders, groups, messages
from test_input_entity_guard import allow, make_client

HUGE = "9" * 5000


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_ALIASES_FILE", str(tmp_path / "aliases.json"))
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", None)

    async def connected(client=None):
        return None

    for module in (runtime, messages, groups, folders):
        monkeypatch.setattr(module, "ensure_connected", connected)


def use(monkeypatch, client):
    for module in (runtime, messages, groups, folders):
        monkeypatch.setattr(module, "get_client", lambda account=None: client)


def test_matcher_never_raises_and_denies(monkeypatch):
    allow(monkeypatch, "5")
    assert runtime._identifier_matches_allowlist({5}, HUGE) is False
    assert runtime._identifier_matches_allowlist({5}, "-" + HUGE) is False
    assert not runtime.is_chat_allowed(HUGE)


@pytest.mark.parametrize("allowlist", ["5", "5, @name_x"])
@pytest.mark.parametrize(
    "spelling", [HUGE, "-" + HUGE, " " + HUGE + " "], ids=["pos", "neg", "pad"]
)
@pytest.mark.asyncio
async def test_validate_id_tool_clean_error_under_allowlist(monkeypatch, allowlist, spelling):
    allow(monkeypatch, allowlist)
    client = make_client()
    use(monkeypatch, client)
    out = await messages.get_messages(chat_id=spelling)
    assert "out of the valid integer range" in out
    assert client.requests == []


@pytest.mark.parametrize("spelling", [HUGE, "-" + HUGE], ids=["pos", "neg"])
@pytest.mark.asyncio
async def test_validate_id_tool_clean_error_without_allowlist(monkeypatch, spelling):
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    client = make_client()
    use(monkeypatch, client)
    out = await messages.get_messages(chat_id=spelling)
    assert "out of the valid integer range" in out
    assert client.requests == []


@pytest.mark.parametrize("allowlist", [None, "5", "*"])
@pytest.mark.parametrize("value", [10**5000, -(10**5000), 2**63], ids=["pos", "neg", "edge"])
@pytest.mark.asyncio
async def test_huge_int_chat_id_is_clean_range_error(monkeypatch, allowlist, value):
    # A JSON integer is parsed to an arbitrary-size Python int; str() of one beyond
    # 4300 digits raises, so the error text must not format the whole value.
    if allowlist is None:
        monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    else:
        allow(monkeypatch, allowlist)
    client = make_client()
    use(monkeypatch, client)
    out = await messages.get_messages(chat_id=value)
    assert "out of the valid integer range" in out
    assert len(out) < 1000
    assert client.requests == []


@pytest.mark.asyncio
async def test_forward_send_as_under_allowlist_is_clean_denial(monkeypatch):
    allow(monkeypatch, "5")
    client = make_client()
    use(monkeypatch, client)
    try:
        out = await messages.forward_message(5, 5, 5, send_as=HUGE)
    except ToolError as error:
        out = str(error)
    assert "4300" not in out
    assert client.requests == []


@pytest.mark.asyncio
async def test_create_folder_under_allowlist_is_clean_denial(monkeypatch):
    allow(monkeypatch, "5")
    client = make_client()
    use(monkeypatch, client)
    try:
        out = await folders.create_folder(title="T", chat_ids=[HUGE])
    except ToolError as error:
        out = str(error)
    assert "4300" not in out
    # Only the folder listing runs before the peer check; no peer is ever queried.
    assert {type(r).__name__ for r in client.requests} <= {"GetDialogFiltersRequest"}


ALLOWLISTS = [None, "5", "*", "5, *"]


def _set_allowlist(monkeypatch, allowlist):
    if allowlist is None:
        monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    else:
        allow(monkeypatch, allowlist)


async def _call(coro):
    try:
        return await coro
    except ToolError as error:
        return str(error)


@pytest.mark.parametrize("allowlist", ALLOWLISTS)
@pytest.mark.asyncio
async def test_list_param_with_one_huge_item_refuses_whole_call(monkeypatch, allowlist):
    _set_allowlist(monkeypatch, allowlist)
    client = make_client()
    use(monkeypatch, client)
    out = await _call(groups.create_group(title="T", user_ids=[5, HUGE, 6], account=None))
    assert "out of the valid integer range" in out
    assert "4300" not in out
    assert client.requests == []


@pytest.mark.parametrize("allowlist", ALLOWLISTS)
@pytest.mark.parametrize("spelling", [HUGE, "-" + HUGE])
@pytest.mark.asyncio
async def test_peer_subject_tool_huge_digits_clean_error(monkeypatch, allowlist, spelling):
    from telegram_mcp.tools import contacts

    _set_allowlist(monkeypatch, allowlist)
    monkeypatch.setattr(contacts, "ensure_connected", lambda client=None: _none())
    client = make_client()
    monkeypatch.setattr(contacts, "get_client", lambda account=None: client)
    use(monkeypatch, client)
    out = await _call(contacts.block_user(user_id=spelling))
    assert "out of the valid integer range" in out
    assert "4300" not in out
    assert client.requests == []


async def _none():
    return None


@pytest.mark.parametrize("allowlist", ALLOWLISTS)
@pytest.mark.asyncio
async def test_raise_errors_variant_raises_clean_range_error(monkeypatch, allowlist):
    _set_allowlist(monkeypatch, allowlist)
    client = make_client()
    use(monkeypatch, client)
    with pytest.raises(ToolError) as excinfo:
        await groups.get_participants(chat_id=HUGE)
    assert "out of the valid integer range" in str(excinfo.value)
    assert "4300" not in str(excinfo.value)
    assert client.requests == []


@pytest.mark.parametrize("allowlist", ALLOWLISTS)
@pytest.mark.parametrize("suffix", ["", "\n", "\r\n\t", " "])
@pytest.mark.asyncio
async def test_error_text_is_bounded_and_has_no_control_chars(monkeypatch, allowlist, suffix):
    _set_allowlist(monkeypatch, allowlist)
    client = make_client()
    use(monkeypatch, client)
    out = await messages.get_messages(chat_id="1" * 5000 + suffix)
    assert "out of the valid integer range" in out
    assert len(out) < 1000
    assert "1" * 41 not in out
    assert all(ch.isprintable() or ch == "\n" for ch in out)
    assert client.requests == []
