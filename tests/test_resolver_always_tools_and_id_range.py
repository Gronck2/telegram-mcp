"""Task 0006 round 4: get_participants / get_recent_actions resolve without an allowlist
too, and a digit string outside the 64-bit id range is a clean error."""

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from telethon import functions, types

import test_phone_routing_no_allowlist_tools as base
from telegram_mcp import runtime
from telegram_mcp.tools import chats, groups, messages

PHONE = base.PHONE
IMPOSTOR_ID = base.IMPOSTOR_ID
BAD_RANGE = "The id is out of the valid id range."


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

    for module in (runtime, messages, groups, chats):
        monkeypatch.setattr(module, "ensure_connected", connected)


def make_client(title=None, contact=None):
    client = base.make_client(title=title, contact=contact)
    inner = client._call

    async def call(sender, request, **kwargs):
        if isinstance(request, functions.channels.GetAdminLogRequest):
            client.requests.append(request)
            return types.channels.AdminLogResults(events=[], chats=[], users=[])
        return await inner(sender, request, **kwargs)

    client._call = call
    return client


def use(monkeypatch, client):
    for module in (runtime, messages, groups, chats):
        monkeypatch.setattr(module, "get_client", lambda account=None: client)


async def run(tool, chat_id):
    if tool == "get_participants":
        return await groups.get_participants(chat_id)
    return await groups.get_recent_actions(chat_id)


TOOLS = ["get_participants", "get_recent_actions"]
FINAL = {
    "get_participants": functions.channels.GetParticipantsRequest,
    "get_recent_actions": functions.channels.GetAdminLogRequest,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("spelling", base.SPELLINGS)
async def test_phone_titled_impostor_no_contact_is_refused(monkeypatch, tool, spelling):
    client = make_client(title=spelling.strip())
    use(monkeypatch, client)
    try:
        out = await run(tool, spelling)
    except ToolError as error:  # get_participants reports failures this way
        out = f"error {error}"
    assert "error" in out.lower() or "not found" in out.lower()
    assert IMPOSTOR_ID not in base.ids_touched(client)
    assert not [r for r in client.requests if isinstance(r, FINAL[tool])]
    assert base.contacts_calls(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_phone_with_contact_resolves_the_contact(monkeypatch, tool):
    client = make_client(title=PHONE, contact=base.CONTACT)
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
        await run(tool, PHONE)
    else:
        await run(tool, PHONE)
        seen = [r.channel for r in client.requests if isinstance(r, FINAL[tool])]
    assert seen, "tool never reached the final call"
    assert all(getattr(p, "user_id", None) == base.CONTACT.id for p in seen)
    assert IMPOSTOR_ID not in base.ids_touched(client)


@pytest.mark.asyncio
async def test_get_participants_int_id_unchanged(monkeypatch):
    client = make_client(title=PHONE)
    use(monkeypatch, client)
    seen = []

    def iter_participants(target, *, limit):
        seen.append(target)

        async def nothing():
            return
            yield

        return nothing()

    client.iter_participants = iter_participants
    await groups.get_participants(base.CHAN.id)
    assert [p.channel_id for p in seen] == [base.CHAN.id]
    assert not base.contacts_calls(client)


@pytest.mark.asyncio
async def test_get_recent_actions_int_id_unchanged(monkeypatch):
    client = make_client(title=PHONE)
    use(monkeypatch, client)
    await groups.get_recent_actions(base.CHAN.id)
    sent = [r for r in client.requests if isinstance(r, FINAL["get_recent_actions"])]
    assert sent and sent[0].channel.channel_id == base.CHAN.id
    assert not base.contacts_calls(client)


HUGE = ["9" * 20, "-" + "9" * 20, str(2**63), str(-(2**63) - 1), "7" * 5000]


@pytest.mark.asyncio
@pytest.mark.parametrize("getter", ["resolve_entity", "resolve_input_entity"])
@pytest.mark.parametrize("value", HUGE)
async def test_out_of_range_digit_string_is_value_error(monkeypatch, getter, value):
    client = make_client()
    use(monkeypatch, client)
    with pytest.raises(ValueError) as info:
        await getattr(runtime, getter)(value, client)
    assert str(info.value) == BAD_RANGE
    assert client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [str(2**63 - 1), str(-(2**63))])
async def test_int64_bounds_still_reach_the_lookup(monkeypatch, value):
    client = make_client()
    use(monkeypatch, client)
    # The in-range extreme goes on to the (fake) lookup, which may fail in any way
    # except with the range refusal.
    try:
        await runtime.resolve_input_entity(value, client)
    except Exception as error:
        assert str(error) != BAD_RANGE
    assert client.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("value", HUGE)
async def test_export_chat_history_huge_digits_clean_error(monkeypatch, tmp_path, value):
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
@pytest.mark.parametrize("value", HUGE[:3])
async def test_resolve_username_tool_huge_digits_clean_error(monkeypatch, value):
    client = make_client()
    use(monkeypatch, client)
    ret = await chats.resolve_username(value)
    assert "struct" not in ret.lower()
    assert not [
        r for r in client.requests if not isinstance(r, functions.contacts.ResolveUsernameRequest)
    ]
