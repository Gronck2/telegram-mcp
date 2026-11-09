"""Round 5 of structured tool errors (task 0007): kinds that were guessed or unrecorded.

Each case drives the real CallToolRequest handler against a stub client: text identical with
the switch OFF, documented kind (plus ``rpc_error`` / ``retry_after``) ON, and a nearby
success that stays untouched.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from telethon import errors, types

from telegram_mcp import runtime, transcription
from telegram_mcp.tools import messages

from test_structured_errors_round3 import _roots
from test_structured_errors_returns import (  # noqa: F401  (_env is an autouse fixture)
    META,
    Pair,
    _env,
    call,
    check_pair,
    fake,
    nothing,
    run,
    text,
    tg,
    user,
)

FLOOD = {"kind": "flood", "retry_after": 31}
RATES = types.PrivacyValueAllowAll()
RULES = SimpleNamespace(rules=[RATES], chats=[], users=[])


def flood():
    return errors.FloodWaitError(request=None, capture=31)


def _privacy_ok():
    return fake({"GetPrivacyRequest": RULES, "SetPrivacyRequest": RULES})


PAIRS = [
    Pair(
        "remove_user",
        "not_found",
        (
            {"chat_id": 100, "user_id": 5},
            lambda: fake({"GetParticipantRequest": tg("UserNotParticipantError")}),
        ),
        "not a member",
        (
            {"chat_id": 100, "user_id": 5},
            lambda: fake(
                {
                    "GetParticipantRequest": SimpleNamespace(
                        participant=types.ChannelParticipant(user_id=5, date=None)
                    ),
                    "EditBannedRequest": None,
                }
            ),
        ),
        "removed from chat",
        extra={"rpc_error": "USER_NOT_PARTICIPANT"},
        id="remove_user-not-participant-rpc",
    ),
    Pair(
        "edit_admin_rights",
        "private",
        (
            {"chat_id": 100, "user_id": 1, "mode": "patch", "change_info": True},
            lambda: fake({"GetParticipantRequest": tg("UserNotParticipantError")}),
        ),
        "you are not in this chat",
        (
            {
                "chat_id": 100,
                "user_id": 5,
                "mode": "patch",
                "change_info": False,
                "delete_messages": True,
            },
            lambda: fake(
                {
                    "GetParticipantRequest": SimpleNamespace(
                        participant=types.ChannelParticipantAdmin(
                            user_id=5,
                            promoted_by=1,
                            date=None,
                            admin_rights=types.ChatAdminRights(change_info=True),
                            can_edit=True,
                        )
                    ),
                    "EditAdminRequest": None,
                }
            ),
        ),
        "Admin rights",
        extra={"rpc_error": "USER_NOT_PARTICIPANT"},
        id="patch-self-not-in-chat",
    ),
    Pair(
        "create_group",
        "flood",
        ({"title": "G", "user_ids": [5, 7]}, lambda: _with_entity(7, flood())),
        "Could not find a requested user",
        (
            {"title": "G", "user_ids": [5]},
            lambda: fake(
                {
                    "CreateChatRequest": SimpleNamespace(
                        chats=[
                            types.Chat(
                                id=42,
                                title="G",
                                photo=types.ChatPhotoEmpty(),
                                participants_count=2,
                                date=None,
                                version=1,
                            )
                        ]
                    )
                }
            ),
        ),
        "Group created",
        extra={"retry_after": 31},
        id="create_group-flood-while-resolving",
    ),
    Pair(
        "set_privacy_settings",
        "flood",
        ({"key": "status", "allow_users": [7]}, lambda: _with_entity(7, flood())),
        "Could not find a requested user",
        ({"key": "status", "allow_users": [5]}, _privacy_ok),
        "updated successfully",
        extra={"retry_after": 31},
        id="set_privacy-flood-while-resolving",
    ),
    Pair(
        "send_file",
        "invalid_argument",
        ({"chat_id": 100, "file_path": "a.png", "custom_emojis": [{"x": 1}]}, lambda: fake()),
        "custom_emojis needs a caption",
        ({"chat_id": 100, "file_path": "a.png"}, lambda: fake(send_file=None)),
        "File sent",
        setup=_roots,
        ok_setup=_roots,
        id="send_file-caption-error",
    ),
    Pair(
        "send_album",
        "invalid_argument",
        (
            {"chat_id": 100, "file_paths": ["a.png", "a.png"], "custom_emojis": [{"x": 1}]},
            lambda: fake(),
        ),
        "custom_emojis needs a caption",
        ({"chat_id": 100, "file_paths": ["a.png", "a.png"]}, lambda: fake(send_file=None)),
        "Album sent",
        setup=_roots,
        ok_setup=_roots,
        id="send_album-caption-error",
    ),
]


def _with_entity(key, value):
    client = fake()
    client.entities[key] = value
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("pair", PAIRS, ids=lambda p: p.id)
async def test_round5_pair(monkeypatch, pair):
    await check_pair(monkeypatch, pair)


@pytest.mark.asyncio
async def test_create_group_unknown_user_is_still_not_found(monkeypatch):
    on = await run(
        monkeypatch,
        "create_group",
        ({"title": "G", "user_ids": [5, 999]}, lambda: fake()),
        nothing,
        True,
    )
    assert on.isError is True
    assert on.meta[META]["kind"] in {"not_found", "invalid_argument"}


@pytest.mark.asyncio
async def test_create_group_alias_needs_user(monkeypatch):
    spec = ({"title": "G", "user_ids": ["неттакой"]}, lambda: fake())
    off = await run(monkeypatch, "create_group", spec, nothing, False)
    on = await run(monkeypatch, "create_group", spec, nothing, True)
    assert text(on) == text(off) and off.meta is None
    assert on.meta == {META: {"kind": "invalid_argument", "needs_user": True}}


@pytest.mark.asyncio
async def test_set_privacy_unresolved_user_is_recorded_once(monkeypatch):
    spec = ({"key": "status", "allow_users": [5, 999]}, lambda: fake())
    on = await run(monkeypatch, "set_privacy_settings", spec, nothing, True)
    assert on.isError is True
    assert on.meta[META]["kind"] != "flood"
    assert "accounts" not in on.meta[META]


@pytest.mark.asyncio
async def test_set_privacy_non_user_entity_is_not_found(monkeypatch):
    spec = ({"key": "status", "allow_users": [100]}, lambda: fake())
    on = await run(monkeypatch, "set_privacy_settings", spec, nothing, True)
    assert on.meta == {META: {"kind": "not_found"}}


def _transcribe_setup(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TRANSCRIBE", "on-demand")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(transcription, "get_cached_transcript", lambda *a, **k: None)
    monkeypatch.setattr(transcription, "is_transcribable", lambda m: True)


@pytest.mark.asyncio
async def test_transcribe_voice_engine_config_error_is_internal(monkeypatch):
    spec = (
        {"chat_id": 100, "message_id": 1, "engine": "groq"},
        lambda: fake(get_messages=SimpleNamespace(id=1)),
    )
    _transcribe_setup(monkeypatch)
    off = await run(monkeypatch, "transcribe_voice", spec, nothing, False)
    on = await run(monkeypatch, "transcribe_voice", spec, nothing, True)
    assert "GROQ_API_KEY is not configured" in text(off)
    assert text(on) == text(off) and (off.isError, off.meta) == (False, None)
    assert on.isError is True and on.meta == {META: {"kind": "internal"}}


@pytest.mark.asyncio
async def test_transcribe_voice_cached_twin_is_unaffected(monkeypatch):
    _transcribe_setup(monkeypatch)
    cached = {"text": "hi", "source": "groq", "duration": 1}
    monkeypatch.setattr(transcription, "get_cached_transcript", lambda *a, **k: cached)
    spec = ({"chat_id": 100, "message_id": 1, "engine": "groq"}, lambda: fake())
    on = await run(monkeypatch, "transcribe_voice", spec, nothing, True)
    assert (on.isError, on.meta) == (False, None)
    assert json.loads(text(on))["text"] == "hi"


# --- fan-out: an escaped failure on one account is recorded for that account --------


@pytest.fixture
def fanout_probe():
    @runtime.mcp.tool(name="zz_round5_probe")
    @runtime.with_account(readonly=True)
    async def tool(account: str = None) -> str:
        if account == "a":
            raise flood()
        await asyncio.sleep(0.01)
        raise RuntimeError("backend exploded")

    yield
    runtime.mcp._tool_manager._tools.pop("zz_round5_probe", None)


@pytest.mark.asyncio
async def test_fanout_records_an_escaped_flood_wait_per_account(monkeypatch, fanout_probe):
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    monkeypatch.setattr(runtime, "clients", {"a": fake(), "b": fake()})
    result = await call("zz_round5_probe")
    assert result.isError is True
    meta = result.meta[META]
    assert meta["accounts"] == [
        {"kind": "flood", "retry_after": 31, "account": "a"},
        {"kind": "internal", "account": "b"},
    ]
    assert meta["kind"] == "internal"


@pytest.mark.asyncio
async def test_fanout_escaped_failure_off_has_no_meta(monkeypatch, fanout_probe):
    monkeypatch.delenv("TELEGRAM_STRUCTURED_ERRORS", raising=False)
    monkeypatch.setattr(runtime, "clients", {"a": fake(), "b": fake()})
    result = await call("zz_round5_probe")
    assert result.meta is None
