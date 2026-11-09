"""get_history paging (min_id, offset_id, since_date) and the truncated flag (task 0008).

Stub clients only. The fake iter_messages honours Telethon's bounds (ids descending,
exclusive offset_id, stop at id <= min_id, reply_to filter), so a wrong parameter
forwarded by the tool changes the result.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.tl import types

from telegram_mcp import runtime
from telegram_mcp.tools import messages

CHAT = -1001234567890
CHANNEL_ID = 1234567890
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _msg(msg_id, date=None, text=None, topic=None):
    return types.Message(
        id=msg_id,
        peer_id=types.PeerChannel(CHANNEL_ID),
        date=date or BASE + timedelta(hours=msg_id),
        message=text if text is not None else f"m{msg_id}",
        reply_to=(
            types.MessageReplyHeader(
                forum_topic=True, reply_to_msg_id=topic, reply_to_top_id=topic
            )
            if topic
            else None
        ),
    )


class FakeClient:
    def __init__(self, msgs):
        self.msgs = sorted(msgs, key=lambda m: -m.id)
        self.iter_calls = []
        self.yielded = 0
        self.get_messages = AsyncMock(return_value=[])

    def iter_messages(self, entity, limit=None, offset_id=0, min_id=0, reply_to=None):
        self.iter_calls.append(
            {"limit": limit, "offset_id": offset_id, "min_id": min_id, "reply_to": reply_to}
        )
        assert limit is not None, "limit=None would yield a single message"

        async def gen():
            count = 0
            for m in self.msgs:
                if offset_id and m.id >= offset_id:
                    continue
                if m.id <= min_id:
                    break
                if reply_to and getattr(m.reply_to, "reply_to_msg_id", None) != reply_to:
                    continue
                if count >= limit:
                    return
                count += 1
                self.yielded += 1
                yield m

        return gen()


@pytest.fixture
def setup(monkeypatch):
    def make(msgs):
        cl = FakeClient(msgs)
        monkeypatch.setattr(messages, "get_client", lambda account=None: cl)
        return cl

    monkeypatch.setattr(
        messages,
        "resolve_entity",
        AsyncMock(
            return_value=types.Channel(
                id=CHANNEL_ID, title="C", photo=types.ChatPhotoEmpty(), date=BASE
            )
        ),
    )
    monkeypatch.setattr(messages, "ensure_connected", AsyncMock())
    monkeypatch.setattr(messages.transcription, "prefetch_transcripts", AsyncMock())
    return make


async def _call(**kw):
    return await messages.get_history(chat_id=CHAT, **kw)


def _ids(result):
    return [r["id"] for r in json.loads(result)["results"]]


@pytest.mark.asyncio
async def test_legacy_path_unchanged(setup):
    cl = setup([_msg(1)])
    cl.get_messages.return_value = [_msg(1)]
    result = await _call(limit=5)
    cl.get_messages.assert_awaited_once()
    assert cl.get_messages.await_args.kwargs == {"limit": 5}
    assert cl.iter_calls == []
    parsed = json.loads(result)
    assert set(parsed) == {"results"}
    assert "truncated" not in parsed["results"][0]


@pytest.mark.asyncio
async def test_min_id_alone(setup):
    cl = setup([_msg(i) for i in range(1, 11)])
    assert _ids(await _call(min_id=7)) == [10, 9, 8]
    assert cl.iter_calls[0]["min_id"] == 7


@pytest.mark.asyncio
async def test_offset_id_alone(setup):
    setup([_msg(i) for i in range(1, 11)])
    result = await _call(offset_id=4, limit=10)
    assert _ids(result) == [3, 2, 1]


@pytest.mark.asyncio
async def test_since_date_alone_and_boundary(setup):
    day = datetime(2026, 3, 2, tzinfo=timezone.utc)
    msgs = [
        _msg(1, day - timedelta(days=1)),
        _msg(2, day - timedelta(seconds=1)),  # 23:59:59 the day before
        _msg(3, day),  # 00:00:00 exactly
        _msg(4, day + timedelta(hours=5)),
    ]
    cl = setup(msgs)
    result = await _call(since_date="2026-03-02")
    assert _ids(result) == [4, 3]
    assert "offset_date" not in cl.iter_calls[0]
    assert cl.yielded == 3  # stopped at the first old message, did not read id 1
    parsed = json.loads(result)
    assert parsed["has_more"] is False and parsed["next_offset_id"] is None


@pytest.mark.asyncio
async def test_since_date_stop_beats_peek(setup):
    # limit+1 peek returns an older message below the date: still no more pages.
    day = datetime(2026, 3, 2, tzinfo=timezone.utc)
    setup([_msg(1, day - timedelta(hours=1)), _msg(2, day), _msg(3, day + timedelta(hours=1))])
    parsed = json.loads(await _call(since_date="2026-03-02", limit=2))
    assert [r["id"] for r in parsed["results"]] == [3, 2]
    assert parsed["has_more"] is False and parsed["next_offset_id"] is None


@pytest.mark.asyncio
async def test_all_three_combined(setup):
    day = datetime(2026, 3, 2, tzinfo=timezone.utc)
    setup([_msg(i, day + timedelta(hours=i - 5)) for i in range(1, 13)])
    # dates: id 5 is 00:00; ids >= 5 qualify; min_id 6 -> ids > 6; offset 11 -> < 11
    parsed = json.loads(await _call(min_id=6, offset_id=11, since_date="2026-03-02", limit=3))
    assert [r["id"] for r in parsed["results"]] == [10, 9, 8]
    assert parsed["has_more"] is True and parsed["next_offset_id"] == 8


@pytest.mark.asyncio
async def test_topic_forwarded_with_bounds(setup):
    cl = setup([_msg(i, topic=7 if i % 2 else 8) for i in range(1, 11)])
    result = await _call(topic_id="7", min_id=2, offset_id=10)
    assert _ids(result) == [9, 7, 5, 3]
    assert cl.iter_calls[0] == {"limit": 101, "offset_id": 10, "min_id": 2, "reply_to": 7}


@pytest.mark.asyncio
async def test_paging_walk_no_gap_no_duplicate(setup):
    setup([_msg(i) for i in range(1, 26)])
    seen, offset, pages = [], None, 0
    while True:
        kw = {"limit": 10, "min_id": 3}
        if offset:
            kw["offset_id"] = offset
        parsed = json.loads(await _call(**kw))
        seen += [r["id"] for r in parsed["results"]]
        pages += 1
        if not parsed["has_more"]:
            assert parsed["next_offset_id"] is None
            break
        offset = parsed["next_offset_id"]
        assert offset == parsed["results"][-1]["id"]
    assert pages == 3
    assert seen == list(range(25, 3, -1))


@pytest.mark.asyncio
async def test_exact_limit_and_limit_plus_one(setup):
    setup([_msg(i) for i in range(1, 6)])
    exact = json.loads(await _call(min_id=0, limit=5))
    assert len(exact["results"]) == 5 and exact["has_more"] is False
    assert exact["next_offset_id"] is None
    more = json.loads(await _call(min_id=0, limit=4))
    assert more["has_more"] is True and more["next_offset_id"] == 2


@pytest.mark.asyncio
async def test_empty_page(setup):
    setup([_msg(1)])
    parsed = json.loads(await _call(offset_id=1))
    assert parsed == {"results": [], "has_more": False, "next_offset_id": None}


@pytest.mark.asyncio
async def test_offset_zero_means_omitted_but_uses_paged_path(setup):
    cl = setup([_msg(1), _msg(2)])
    parsed = json.loads(await _call(offset_id=0))
    assert [r["id"] for r in parsed["results"]] == [2, 1]
    assert cl.iter_calls[0]["offset_id"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kw,text",
    [
        ({"min_id": -1}, "Error: min_id must be an integer >= 0."),
        ({"offset_id": -5}, "Error: offset_id must be an integer >= 0."),
        ({"min_id": True}, "Error: min_id must be an integer >= 0."),
        ({"since_date": "2025-1-1"}, "Invalid since_date format. Use YYYY-MM-DD."),
        ({"since_date": "2025-01-01T00:00:00"}, "Invalid since_date format. Use YYYY-MM-DD."),
        ({"since_date": ""}, "Invalid since_date format. Use YYYY-MM-DD."),
        ({"min_id": 1, "limit": 0}, "Error: limit must be an integer >= 1."),
    ],
)
async def test_validation_records_and_makes_no_call(setup, monkeypatch, kw, text):
    cl = setup([_msg(1)])
    get_client = MagicMock(side_effect=AssertionError("network"))
    monkeypatch.setattr(messages, "get_client", get_client)
    recorder = MagicMock()
    monkeypatch.setattr(messages, "record_tool_error", recorder)
    assert await _call(**kw) == text
    recorder.assert_called_once_with("invalid_argument")
    get_client.assert_not_called()
    assert cl.iter_calls == []


@pytest.mark.asyncio
async def test_legacy_limit_zero_not_rejected(setup):
    cl = setup([])
    await _call(limit=0)
    cl.get_messages.assert_awaited_once()


@pytest.mark.asyncio
async def test_bad_topic_id_keeps_old_text(setup):
    cl = setup([_msg(1)])
    assert await _call(topic_id=0, min_id=1) == "Error: topic_id must be a positive integer."
    assert cl.iter_calls == []


def test_read_only_annotation():
    tool = runtime.mcp._tool_manager.get_tool("get_history")
    assert tool.annotations.readOnlyHint is True


@pytest.mark.asyncio
async def test_fan_out_each_block_has_own_paging(setup, monkeypatch):
    clients = {"a": FakeClient([_msg(i) for i in range(1, 4)]), "b": FakeClient([_msg(1)])}
    setup([])
    monkeypatch.setattr(runtime, "clients", clients)
    monkeypatch.setattr(messages, "get_client", lambda account=None: clients[account])
    out = await _call(min_id=0, limit=2)
    text = out if isinstance(out, str) else str(out)
    blocks = {}
    for part in text.split("\n\n[")[0:]:
        label, _, body = part.lstrip("[").partition("]\n")
        blocks[label] = json.loads(body)
    assert blocks["a"]["has_more"] is True and blocks["a"]["next_offset_id"] == 2
    assert blocks["b"]["has_more"] is False


# --- truncated flag ---------------------------------------------------------------


def _record(text):
    return messages.message_to_dict(_msg(1, text=text))


def test_clipped_text_is_flagged():
    rec = _record("a" * 5000)
    assert rec["truncated"] is True
    assert rec["text"] == "a" * 4096 + "... [truncated]"
    assert list(rec).index("truncated") == list(rec).index("text") + 1


def test_exact_limit_and_short_not_flagged():
    assert "truncated" not in _record("a" * 4096)
    assert "truncated" not in _record("short")


def test_user_marker_text_not_flagged():
    rec = _record("hello... [truncated]")
    assert rec["text"] == "hello... [truncated]" and "truncated" not in rec


def test_rich_clip_is_flagged(monkeypatch):
    monkeypatch.setattr(messages, "rich_message_text", lambda msg: "b" * 5000)
    rec = messages.message_to_dict(_msg(1, text=""))
    assert rec["rich"] is True and rec["truncated"] is True
    assert rec["text"].endswith("... [truncated]")
