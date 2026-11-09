"""Task 0006 round 4 tester pass: int64 boundaries across resolvers and tools, and
get_participants / get_recent_actions without an allowlist for non-phone references."""

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from telethon import functions

import test_phone_routing_no_allowlist_tools as base
import test_resolver_always_tools_and_id_range as rr
from telegram_mcp import runtime
from telegram_mcp.tools import chats, groups, messages

_env = rr._env  # autouse fixture, re-registered in this module
make_client = rr.make_client
use = rr.use

MAX = 2**63 - 1
MIN = -(2**63)
BAD_RANGE = rr.BAD_RANGE
OUT = [str(MAX + 1), str(MIN - 1)]
BOUNDS = [str(MAX), str(MIN), f" {MAX} ", f"-{2**63}"]


@pytest.fixture(autouse=True)
def _env_again(_env):
    yield


@pytest.mark.asyncio
@pytest.mark.parametrize("getter", ["resolve_entity", "resolve_input_entity"])
@pytest.mark.parametrize("value", OUT)
async def test_one_beyond_is_refused_without_request(monkeypatch, getter, value):
    client = make_client()
    use(monkeypatch, client)
    with pytest.raises(ValueError) as info:
        await getattr(runtime, getter)(value, client)
    assert str(info.value) == BAD_RANGE
    assert value.strip("-") not in str(info.value)
    assert client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("getter", ["resolve_entity", "resolve_input_entity"])
@pytest.mark.parametrize("value", BOUNDS)
async def test_boundaries_pass_the_range_check(monkeypatch, getter, value):
    client = make_client()
    use(monkeypatch, client)
    try:
        await getattr(runtime, getter)(value, client)
    except Exception as error:
        assert BAD_RANGE not in str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", OUT)
async def test_validated_tool_refuses_same_bound_without_request(monkeypatch, value):
    client = make_client()
    use(monkeypatch, client)
    out = await messages.send_reaction(chat_id=value, message_id=1, emoji="x")
    assert "range" in out.lower()
    assert client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", BOUNDS)
async def test_validated_tool_accepts_bounds(monkeypatch, value):
    client = make_client()
    use(monkeypatch, client)
    out = await messages.send_reaction(chat_id=value, message_id=1, emoji="x")
    assert "valid integer range" not in out
    assert BAD_RANGE not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("value", OUT)
async def test_unvalidated_tool_refuses_same_bound(monkeypatch, tmp_path, value):
    client = make_client()
    use(monkeypatch, client)
    out = tmp_path / "e.json"

    async def writable(*a, **k):
        return out, None

    monkeypatch.setattr(messages, "_resolve_writable_file_path", writable)
    ret = await messages.export_chat_history(chat_id=value, output_path=str(out))
    assert "struct" not in ret.lower()
    assert client.requests == []
    assert value not in ret


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", rr.TOOLS)
@pytest.mark.parametrize("value", OUT)
async def test_participant_admin_log_refuse_out_of_range(monkeypatch, tool, value):
    client = make_client()
    use(monkeypatch, client)
    try:
        out = await rr.run(tool, value)
    except ToolError as error:
        out = f"error {error}"
    assert "error" in out.lower() or "range" in out.lower()
    assert "struct" not in out.lower()
    assert value not in out
    assert client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", rr.TOOLS)
async def test_username_resolves_to_the_user_without_allowlist(monkeypatch, tool):
    client = make_client()
    use(monkeypatch, client)
    seen = []
    if tool == "get_participants":

        def iter_participants(target, *, limit):
            seen.append(target)

            async def nothing():
                return
                yield

            return nothing()

        client.iter_participants = iter_participants
    await rr.run(tool, "@alice")
    if tool == "get_participants":
        assert [getattr(p, "user_id", None) for p in seen] == [base.ALICE.id]
    else:
        sent = [r for r in client.requests if isinstance(r, rr.FINAL[tool])]
        assert [getattr(r.channel, "user_id", None) for r in sent] == [base.ALICE.id]


@pytest.mark.asyncio
async def test_free_text_title_still_resolves_by_title_without_allowlist(monkeypatch):
    client = make_client(title="Team Room")
    use(monkeypatch, client)
    seen = []

    def iter_participants(target, *, limit):
        seen.append(target)

        async def nothing():
            return
            yield

        return nothing()

    client.iter_participants = iter_participants
    await groups.get_participants("Team Room")
    assert seen and getattr(seen[0], "channel_id", None) == base.IMPOSTOR_ID


@pytest.mark.asyncio
async def test_marked_id_and_paging_args_unaffected(monkeypatch):
    client = make_client()
    use(monkeypatch, client)
    limits = []
    seen = []

    def iter_participants(target, *, limit):
        seen.append(target)
        limits.append(limit)

        async def some():
            for u in (base.ALICE, base.SMALL, base.BIG):
                yield u

        return some()

    client.iter_participants = iter_participants
    out = await groups.get_participants(-1000000000000 - base.CHAN.id, page=2, page_size=1)
    assert limits == [3]
    assert [p.channel_id for p in seen] == [base.CHAN.id]
    assert "Small" in out and "Alice" not in out
