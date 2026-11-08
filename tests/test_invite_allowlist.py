"""Invite joins must respect the chat allowlists (task 0001)."""

import pytest
from telethon.tl import functions, types

from telegram_mcp import runtime
from telegram_mcp.tools import groups

ENV_VARS = (
    "TELEGRAM_ALLOWED_CHAT_IDS",
    "TELEGRAM_ALLOWED_READ_CHATS",
    "TELEGRAM_ALLOWED_WRITE_CHATS",
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", None)


class _FakeClient:
    def __init__(self, check_result, import_error=None):
        self.check_result = check_result
        self.import_error = import_error
        self.checks = 0
        self.imports = 0

    async def __call__(self, request):
        if isinstance(request, functions.messages.CheckChatInviteRequest):
            self.checks += 1
            if isinstance(self.check_result, Exception):
                raise self.check_result
            return self.check_result
        if isinstance(request, functions.messages.ImportChatInviteRequest):
            self.imports += 1
            if self.import_error is not None:
                raise self.import_error
            return types.Updates(
                updates=[],
                users=[],
                chats=[types.Chat(**_CHAT_KW(title="Joined"))],
                date=None,
                seq=0,
            )
        raise AssertionError(f"unexpected request {request!r}")

    async def is_user_authorized(self):
        return True

    def is_connected(self):
        return True


def _CHAT_KW(title="T", id=111):
    return dict(
        id=id,
        title=title,
        photo=types.ChatPhotoEmpty(),
        participants_count=1,
        date=None,
        version=1,
    )


def _channel(id=222):
    return types.Channel(
        id=id,
        title="Chan",
        photo=types.ChatPhotoEmpty(),
        date=None,
        megagroup=True,
    )


def _already(chat):
    return types.ChatInviteAlready(chat=chat)


def _plain():
    return types.ChatInvite(
        title="New",
        photo=types.PhotoEmpty(id=0),
        participants_count=1,
        participants=[],
        color=0,
    )


async def _call(tool, client, monkeypatch):
    monkeypatch.setattr(runtime, "get_client", lambda account=None: client)
    monkeypatch.setattr(groups, "get_client", lambda account=None: client)

    async def _noop(cl):
        return None

    monkeypatch.setattr(groups, "ensure_connected", _noop)
    if tool == "link":
        return await groups.join_chat_by_link(link="https://t.me/+abcDEF")
    return await groups.import_chat_invite(hash="+abcDEF")


TOOLS = ["link", "hash"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_no_allowlist_does_not_check_or_block(monkeypatch, tool):
    client = _FakeClient(RuntimeError("invite not resolvable"))
    result = await _call(tool, client, monkeypatch)
    assert "Successfully joined" in result
    assert client.imports == 1
    assert client.checks == 1  # only the tools' own pre-existing probe


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_allowed_already_invite_passes_gate(monkeypatch, tool):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "-1000000000222")
    client = _FakeClient(_already(_channel(222)))
    result = await _call(tool, client, monkeypatch)
    assert "already a member" in result
    assert client.imports == 0
    assert client.checks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_non_allowed_chat_is_refused(monkeypatch, tool):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "-1000000000999")
    client = _FakeClient(_already(_channel(222)))
    result = await _call(tool, client, monkeypatch)
    assert "restricted by privacy policy" in result
    assert client.imports == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_read_only_allowed_chat_is_refused(monkeypatch, tool):
    monkeypatch.setenv("TELEGRAM_ALLOWED_READ_CHATS", "-1000000000222")
    client = _FakeClient(_already(_channel(222)))
    result = await _call(tool, client, monkeypatch)
    assert "Write access" in result and "restricted" in result
    assert client.imports == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_peek_invite_checks_its_chat(monkeypatch, tool):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "-1000000000999")
    client = _FakeClient(types.ChatInvitePeek(chat=_channel(222), expires=0))
    result = await _call(tool, client, monkeypatch)
    assert "restricted by privacy policy" in result
    assert client.imports == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_plain_invite_is_refused_under_allowlist(monkeypatch, tool):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "*")
    client = _FakeClient(_plain())
    result = await _call(tool, client, monkeypatch)
    assert "restricted by privacy policy" in result
    assert client.imports == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_lookup_failure_does_not_join(monkeypatch, tool):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "*")
    client = _FakeClient(RuntimeError("boom"))
    await _call(tool, client, monkeypatch)
    assert client.imports == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_allowed_peek_invite_passes_gate(monkeypatch, tool):
    monkeypatch.setenv("TELEGRAM_ALLOWED_WRITE_CHATS", "-1000000000222")
    client = _FakeClient(types.ChatInvitePeek(chat=_channel(222), expires=0))
    result = await _call(tool, client, monkeypatch)
    assert "Successfully joined" in result
    assert client.imports == 1
    assert client.checks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_peek_invite_joins_without_allowlist(monkeypatch, tool):
    client = _FakeClient(types.ChatInvitePeek(chat=_channel(222), expires=0))
    result = await _call(tool, client, monkeypatch)
    assert "Successfully joined" in result
    assert client.imports == 1
    assert client.checks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_already_invite_without_allowlist_is_not_reimported(monkeypatch, tool):
    client = _FakeClient(_already(_channel(222)))
    result = await _call(tool, client, monkeypatch)
    assert "already a member" in result
    assert client.imports == 0
    assert client.checks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_gate_runs_before_any_import_when_lookup_raises(monkeypatch, tool):
    """CheckChatInvite raising under an allowlist must never lead to an import."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_WRITE_CHATS", "-1000000000222")
    client = _FakeClient(RuntimeError("flood"))
    result = await _call(tool, client, monkeypatch)
    assert client.imports == 0
    assert "Successfully joined" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["@", ",", "@,@", "@*"])
@pytest.mark.parametrize("tool", TOOLS)
async def test_typo_allowlist_refuses_invites(monkeypatch, tool, raw):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", raw)
    client = _FakeClient(_already(_channel(222)))
    result = await _call(tool, client, monkeypatch)
    assert "restricted by privacy policy" in result
    assert client.imports == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_plain_invite_joins_once_without_allowlist(monkeypatch, tool):
    client = _FakeClient(_plain())
    result = await _call(tool, client, monkeypatch)
    assert "Successfully joined" in result
    assert client.imports == 1
    assert client.checks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_plain_invite_not_reported_as_member_under_wildcard_ok(monkeypatch, tool):
    """Under an allowlist an unresolvable plain invite is refused, never imported."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "-1000000000222")
    client = _FakeClient(_plain())
    result = await _call(tool, client, monkeypatch)
    assert "restricted by privacy policy" in result
    assert client.imports == 0
    assert client.checks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    "err,expected",
    [
        ("INVITE_HASH_EXPIRED", "expired"),
        ("INVITE_HASH_INVALID", "invalid or malformed"),
    ],
)
async def test_check_error_without_allowlist_falls_through_to_import(
    monkeypatch, tool, err, expected
):
    """Pre-existing behaviour: a failing probe is ignored, import reports the error."""
    client = _FakeClient(RuntimeError(err), import_error=RuntimeError(err))
    result = await _call(tool, client, monkeypatch)
    assert expected in result
    assert client.checks == 1
    assert client.imports == 1


@pytest.mark.asyncio
async def test_link_check_error_under_allowlist_keeps_friendly_message(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "*")
    client = _FakeClient(RuntimeError("INVITE_HASH_EXPIRED"))
    result = await _call("link", client, monkeypatch)
    assert "expired" in result
    assert client.imports == 0
    assert client.checks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_allowed_peek_import_error_is_reported(monkeypatch, tool):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "-1000000000222")
    client = _FakeClient(
        types.ChatInvitePeek(chat=_channel(222), expires=0),
        import_error=RuntimeError("INVITE_HASH_EXPIRED"),
    )
    result = await _call(tool, client, monkeypatch)
    assert "expired" in result
    assert client.imports == 1
    assert client.checks == 1
