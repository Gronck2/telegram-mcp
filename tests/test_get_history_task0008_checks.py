"""Tester checks for task 0008: legacy equivalence, sanitizer equivalence, Telethon
paging semantics, structured errors.

* tests/data/get_history_legacy_47a3908.json and sanitize_user_content_47a3908.json were
  recorded from a detached worktree of 47a3908 (before task 0008); the current code must
  reproduce them byte for byte.
* The fake iter_messages mirrors telethon 1.45 `_MessagesIter` (non-reverse).
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams
from telethon.tl import types

import _legacy_history_probe as probe
from _sanitize_equiv_inputs import equivalence_cases
from sanitize import sanitize_user_content
from telegram_mcp import runtime
from telegram_mcp.tools import messages

DATA = Path(__file__).parent / "data"
META = "telegram-mcp/error"
CHAT = probe.CHAT


# --- 1. legacy path is byte-identical to the base --------------------------------------


@pytest.mark.asyncio
async def test_legacy_output_and_calls_match_base(monkeypatch):
    out = await probe.run_probe(monkeypatch.setattr)
    golden = json.loads((DATA / "get_history_legacy_47a3908.json").read_text())
    assert out == golden


def test_legacy_golden_covers_the_scenarios():
    golden = json.loads((DATA / "get_history_legacy_47a3908.json").read_text())
    for name in ("plain", "forum", "forum_topic", "rich", "fanout", "fanout_one"):
        assert name in golden
    assert golden["fanout"].startswith("[a]\n") and "\n\n[b]\n" in golden["fanout"]
    # a bad topic_id is refused with the base text and makes no get_messages call
    for key in ("topic_0", "topic_-1", "topic_'abc'", "topic_''", "topic_[]"):
        assert golden[key] == "Error: topic_id must be a positive integer."
        assert golden[key + "|call"] is None
    assert '"truncated"' not in json.dumps(golden)  # nothing in these histories is clipped


# --- 2. sanitize_user_content is unchanged ----------------------------------------------


def test_sanitize_user_content_matches_base():
    golden = json.loads((DATA / "sanitize_user_content_47a3908.json").read_text())
    cases = equivalence_cases()
    assert len(golden) == len(cases) > 600
    got = [hashlib.sha256(sanitize_user_content(t, **k).encode()).hexdigest() for t, k in cases]
    assert got == golden


# --- 3. paging against telethon 1.45 semantics -----------------------------------------


def _msg(i, date=None, topic=None, top=None):
    return types.Message(
        id=i,
        peer_id=types.PeerChannel(probe.CID),
        date=date or probe.BASE + timedelta(hours=i),
        message=f"m{i}",
        reply_to=(
            types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=topic, reply_to_top_id=top)
            if topic
            else None
        ),
    )


class TelethonLikeClient:
    """iter_messages as telethon 1.45 `_MessagesIter` does it for reverse=False.

    - ids go newest -> oldest, offset_id is an exclusive upper bound;
    - min_id is never sent to Telegram: the iterator stops at the first id <= min_id, and
      yields nothing at once when offset_id and min_id are both set and
      offset_id - min_id <= 1;
    - reply_to uses messages.getReplies, which returns the whole thread, nested replies
      included (reply_to_top_id == topic), so the match is on either header field;
    - limit counts yielded messages; limit=None would yield a single message.
    """

    def __init__(self, msgs):
        self.msgs = sorted(msgs, key=lambda m: -m.id)
        self.calls = []

    def iter_messages(
        self, entity, limit=None, offset_id=0, min_id=0, reply_to=None, **unsupported
    ):
        assert not unsupported, unsupported
        assert limit is not None
        self.calls.append(dict(limit=limit, offset_id=offset_id, min_id=min_id, reply_to=reply_to))

        async def gen():
            if offset_id and min_id and offset_id - min_id <= 1:
                return
            n = 0
            for m in self.msgs:
                if offset_id and m.id >= offset_id:
                    continue
                if m.id <= min_id:
                    return
                h = m.reply_to
                if reply_to and reply_to not in (
                    getattr(h, "reply_to_msg_id", None),
                    getattr(h, "reply_to_top_id", None),
                ):
                    continue
                if n >= limit:
                    return
                n += 1
                yield m

        return gen()


@pytest.fixture
def hist(monkeypatch):
    def make(msgs, forum=False):
        cl = TelethonLikeClient(msgs)
        monkeypatch.setattr(messages, "get_client", lambda account=None: cl)
        monkeypatch.setattr(
            messages, "resolve_entity", AsyncMock(return_value=probe._entity(forum))
        )
        monkeypatch.setattr(messages, "ensure_connected", AsyncMock())
        monkeypatch.setattr(messages.transcription, "prefetch_transcripts", AsyncMock())
        monkeypatch.setattr(messages, "topic_titles", AsyncMock(return_value={}))
        return cl

    return make


async def _get(**kw):
    return json.loads(await messages.get_history(chat_id=CHAT, **kw))


async def _walk(**kw):
    seen, offset, pages = [], None, 0
    while True:
        args = dict(kw)
        if offset:
            args["offset_id"] = offset
        page = await _get(**args)
        seen += [r["id"] for r in page["results"]]
        pages += 1
        if not page["has_more"]:
            assert page["next_offset_id"] is None
            return seen, pages
        assert page["next_offset_id"] == page["results"][-1]["id"]
        assert page["next_offset_id"] < (offset or 10**9)  # always progresses
        offset = page["next_offset_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2, 3, 7, 10, 19, 20, 100])
async def test_walk_covers_every_id_once(hist, limit):
    hist([_msg(i) for i in range(1, 21)])
    seen, pages = await _walk(min_id=0, limit=limit)
    assert seen == list(range(20, 0, -1))
    assert pages == -(-20 // limit)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 3, 4])
async def test_walk_stops_at_min_id_with_gaps_in_ids(hist, limit):
    ids = [2, 3, 5, 8, 9, 13, 14, 15, 21]  # deleted messages leave id gaps
    hist([_msg(i) for i in ids])
    seen, _ = await _walk(min_id=5, limit=limit)
    assert seen == [21, 15, 14, 13, 9, 8]


@pytest.mark.asyncio
async def test_walk_with_topic_includes_nested_replies(hist):
    # 10 is the topic root's child, 11 a nested reply (reply_to_msg_id=10, top=7)
    msgs = [_msg(i, topic=7, top=7) for i in (8, 9, 12)] + [_msg(11, topic=10, top=7)]
    msgs += [_msg(i, topic=4, top=4) for i in (10, 13)]
    cl = hist(msgs, forum=True)
    seen, _ = await _walk(topic_id=7, min_id=0, limit=2)
    assert seen == [12, 11, 9, 8]
    assert all(c["reply_to"] == 7 for c in cl.calls)


@pytest.mark.asyncio
async def test_offset_and_min_id_adjacent_yield_nothing(hist):
    hist([_msg(i) for i in range(1, 6)])
    assert await _get(offset_id=4, min_id=3) == {
        "results": [],
        "has_more": False,
        "next_offset_id": None,
    }


@pytest.mark.asyncio
async def test_offset_id_is_exclusive_and_min_id_is_exclusive(hist):
    hist([_msg(i) for i in range(1, 6)])
    page = await _get(offset_id=5, min_id=1)
    assert [r["id"] for r in page["results"]] == [4, 3, 2]


@pytest.mark.asyncio
async def test_limit_plus_one_peek_is_what_is_sent(hist):
    cl = hist([_msg(i) for i in range(1, 6)])
    await _get(min_id=0, limit=2)
    assert cl.calls == [dict(limit=3, offset_id=0, min_id=0, reply_to=None)]


@pytest.mark.asyncio
async def test_since_date_midnight_inclusive_and_second_before_excluded(hist):
    day = datetime(2026, 3, 2, tzinfo=timezone.utc)
    hist([_msg(1, day - timedelta(microseconds=1)), _msg(2, day), _msg(3, day.replace(hour=23))])
    page = await _get(since_date="2026-03-02")
    assert [r["id"] for r in page["results"]] == [3, 2]
    assert page["has_more"] is False and page["next_offset_id"] is None


@pytest.mark.asyncio
async def test_since_date_non_utc_message_dates_compare_as_instants(hist):
    tz = timezone(timedelta(hours=3))
    # 2026-03-02 02:00 +03:00 is 2026-03-01 23:00 UTC: before the UTC day start
    hist(
        [_msg(1, datetime(2026, 3, 2, 2, tzinfo=tz)), _msg(2, datetime(2026, 3, 2, 3, tzinfo=tz))]
    )
    page = await _get(since_date="2026-03-02")
    assert [r["id"] for r in page["results"]] == [2]


@pytest.mark.asyncio
async def test_since_date_in_the_future_and_page_boundary(hist):
    hist([_msg(i) for i in range(1, 4)])
    page = await _get(since_date="2999-01-01")
    assert page == {"results": [], "has_more": False, "next_offset_id": None}


@pytest.mark.asyncio
async def test_since_date_walk_pages_then_ends_at_the_date(hist):
    day = datetime(2026, 3, 2, tzinfo=timezone.utc)
    hist([_msg(i, day + timedelta(hours=i - 4)) for i in range(1, 11)])  # id 4 = 00:00
    seen, pages = await _walk(since_date="2026-03-02", limit=3)
    assert seen == [10, 9, 8, 7, 6, 5, 4]
    assert pages == 3


@pytest.mark.asyncio
async def test_paged_records_keep_message_shape(hist):
    hist([_msg(2), _msg(1)])
    page = await _get(min_id=0)
    assert set(page) == {"results", "has_more", "next_offset_id"}
    assert page["results"][0] == {
        "id": 2,
        "sender": "Unknown",
        "date": (probe.BASE + timedelta(hours=2)).isoformat(),
        "text": "m2",
    }


def test_get_history_registered_once_with_new_parameters():
    tool = runtime.mcp._tool_manager.get_tool("get_history")
    props = tool.parameters["properties"]
    assert {"min_id", "offset_id", "since_date", "topic_id", "limit"} <= set(props)
    assert "account" in props and "chat_id" in props


def test_get_history_decorators_applied_once():
    import inspect

    src = inspect.getsource(messages)
    head = src[: src.index("async def get_history(")]
    tail = head[head.rindex("def _fetch_history_page") :]
    assert tail.count("@mcp.tool(") == 1
    assert tail.count("@with_account(") == 1
    assert tail.count('@validate_id("chat_id")') == 1


@pytest.mark.asyncio
async def test_registered_get_history_is_single_wrapped_and_listed_with_new_params():
    from mcp.types import ListToolsRequest

    fn = runtime.mcp._tool_manager.get_tool("get_history").fn
    chain = []
    while hasattr(fn, "__wrapped__"):
        chain.append(fn)
        fn = fn.__wrapped__
    assert fn.__qualname__ == "get_history"
    assert len(chain) == len({id(f) for f in chain})
    # with_account + validate_id once each: no wrapper layer repeats the same code object
    codes = [getattr(f, "__code__", None) for f in chain]
    assert len(codes) == len(set(codes))

    handler = runtime.mcp._mcp_server.request_handlers[ListToolsRequest]
    listed = (await handler(ListToolsRequest(method="tools/list"))).root.tools
    matches = [t for t in listed if t.name == "get_history"]
    assert len(matches) == 1
    props = matches[0].inputSchema["properties"]
    assert {"min_id", "offset_id", "since_date"} <= set(props)


# --- 4. structured errors ---------------------------------------------------------------


async def _call(**arguments):
    request = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name="get_history", arguments={"chat_id": CHAT, **arguments}),
    )
    handler = runtime.mcp._mcp_server.request_handlers[CallToolRequest]
    return (await handler(request)).root


BAD = [
    ({"min_id": -1}, "Error: min_id must be an integer >= 0."),
    ({"offset_id": -1}, "Error: offset_id must be an integer >= 0."),
    ({"since_date": "2026-03-02T10:00:00"}, "Invalid since_date format. Use YYYY-MM-DD."),
    ({"since_date": "02.03.2026"}, "Invalid since_date format. Use YYYY-MM-DD."),
    ({"min_id": 1, "limit": 0}, "Error: limit must be an integer >= 1."),
    ({"min_id": 1, "limit": -3}, "Error: limit must be an integer >= 1."),
    ({"min_id": 1, "topic_id": 0}, "Error: topic_id must be a positive integer."),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("kw,expected", BAD)
async def test_invalid_input_structured_on(hist, monkeypatch, kw, expected):
    cl = hist([_msg(1)])
    monkeypatch.setattr(messages, "get_client", lambda account=None: cl)
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    result = await _call(**kw)
    assert result.isError is True
    assert result.content[0].text == expected
    assert result.meta[META]["kind"] == "invalid_argument"
    assert cl.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kw,expected", BAD)
async def test_invalid_input_structured_off(hist, monkeypatch, kw, expected):
    cl = hist([_msg(1)])
    monkeypatch.delenv("TELEGRAM_STRUCTURED_ERRORS", raising=False)
    result = await _call(**kw)
    assert not result.isError
    assert result.meta is None
    assert result.content[0].text == expected
    assert cl.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["1", None])
async def test_valid_paged_call_is_not_an_error(hist, monkeypatch, flag):
    hist([_msg(2), _msg(1)])
    if flag:
        monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", flag)
    else:
        monkeypatch.delenv("TELEGRAM_STRUCTURED_ERRORS", raising=False)
    result = await _call(min_id=0)
    assert not result.isError and result.meta is None
    assert json.loads(result.content[0].text)["has_more"] is False


@pytest.mark.asyncio
async def test_non_integer_ids_rejected_by_schema_or_tool(hist, monkeypatch):
    cl = hist([_msg(1)])
    monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    result = await _call(min_id="abc")
    assert result.isError is True
    assert cl.calls == []
