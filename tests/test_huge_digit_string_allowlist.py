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
