"""Scenarios for the get_history legacy-path golden (task 0008).

`run_probe(patch)` drives get_history WITHOUT min_id/offset_id/since_date over several
stub histories and returns {scenario: output, scenario|call: get_messages kwargs}.
tests/data/get_history_legacy_47a3908.json was recorded by running it, unchanged, on a
detached worktree of 47a3908 (task 0007 merged, before task 0008):

    asyncio.run(run_probe(setattr)) with cwd = that worktree on sys.path

`patch(obj, name, value)` is monkeypatch.setattr or a plain setattr.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from telethon.tl import types

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
CID = 1234567890
CHAT = -1001234567890


def _m(i, text=None, topic=None, top=None, rich=None):
    msg = types.Message(
        id=i,
        peer_id=types.PeerChannel(CID),
        date=BASE + timedelta(hours=i),
        message=text if text is not None else f"m{i}",
        reply_to=(
            types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=topic, reply_to_top_id=top)
            if topic
            else None
        ),
    )
    msg._rich = rich
    return msg


def _entity(forum):
    return types.Channel(id=CID, title="C", photo=types.ChatPhotoEmpty(), date=BASE, forum=forum)


class _Client:
    def __init__(self, msgs):
        self.get_messages = AsyncMock(return_value=msgs)


async def run_probe(patch):
    from telegram_mcp import runtime
    from telegram_mcp.tools import messages

    patch(messages, "rich_message_text", lambda msg: getattr(msg, "_rich", None) or "")
    patch(messages, "ensure_connected", AsyncMock())
    patch(messages.transcription, "prefetch_transcripts", AsyncMock())
    patch(messages, "topic_titles", AsyncMock(return_value={"3": "Topic three"}))
    out = {}

    async def run(name, msgs, forum=False, **kw):
        cl = _Client(msgs)
        patch(messages, "get_client", lambda account=None: cl)
        patch(messages, "resolve_entity", AsyncMock(return_value=_entity(forum)))
        out[name] = await messages.get_history(chat_id=CHAT, **kw)
        args = cl.get_messages.await_args
        out[name + "|call"] = None if args is None else args.kwargs

    plain = [_m(3), _m(2, text="hi ​ there\x07 \U0001f600 ‮ x"), _m(1, text="")]
    await run("plain", plain, limit=10)
    await run("plain_default", plain)
    await run("empty", [], limit=5)
    await run("limit0", [], limit=0)
    forum = [_m(5, topic=3), _m(4, topic=3, top=3), _m(3, text="x" * 4096), _m(2, topic=7)]
    await run("forum", forum, forum=True, limit=10)
    await run("forum_topic", forum, forum=True, limit=10, topic_id=3)
    await run("forum_topic_str", forum, forum=True, topic_id="3")
    rich = [_m(2, text="", rich="para1\n\npara2"), _m(1, text="", rich="r" * 4000)]
    await run("rich", rich, limit=3)
    for bad in (0, -1, "abc", "", [], 1.5):
        await run(f"topic_{bad!r}", [_m(1)], topic_id=bad)
    clients = {"a": _Client([_m(2), _m(1)]), "b": _Client([_m(1, text="b!")])}
    patch(runtime, "clients", clients)
    patch(messages, "get_client", lambda account=None: clients[account])
    patch(messages, "resolve_entity", AsyncMock(return_value=_entity(False)))
    out["fanout"] = await messages.get_history(chat_id=CHAT, limit=5)
    out["fanout_one"] = await messages.get_history(chat_id=CHAT, limit=5, account="b")
    return out
