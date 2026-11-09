"""An id that came from a saved alias is exact: no marked-variant guessing (task 0010)."""

import json

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams

from telegram_mcp import runtime

ID = 1234567
CHANNEL = -1000000000000 - ID
GROUP = -ID
WORDING = "мой контакт"
META = "telegram-mcp/error"


class FakeClient:
    """Stub client: resolves only ids in ``known``, records every lookup."""

    def __init__(self, known=()):
        self.known = set(known)
        self.calls = []
        self.dialogs_loaded = 0

    async def get_dialogs(self):
        self.dialogs_loaded += 1

    async def get_entity(self, identifier):
        return self._look("get_entity", identifier)

    async def get_input_entity(self, identifier):
        return self._look("get_input_entity", identifier)

    def _look(self, getter, identifier):
        self.calls.append(identifier)
        if identifier in self.known:
            return f"{getter}:{identifier}"
        raise ValueError(f"Could not find the input entity for {identifier!r}")

    def asked_marked(self):
        return [c for c in self.calls if c in (CHANNEL, GROUP)]


def marked_peers():
    return FakeClient({CHANNEL, GROUP})


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for name in (
        "TELEGRAM_ALLOWED_CHAT_IDS",
        "TELEGRAM_ALLOWED_READ_CHATS",
        "TELEGRAM_ALLOWED_WRITE_CHATS",
        "TELEGRAM_STRUCTURED_ERRORS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", None)
    monkeypatch.setenv("TELEGRAM_ALIASES_FILE", str(tmp_path / "aliases.json"))
    runtime.save_aliases({WORDING: ID})

    async def connected(client=None):
        return None

    monkeypatch.setattr(runtime, "ensure_connected", connected)


GETTERS = [runtime.resolve_entity, runtime.resolve_input_entity]


def assert_stale(error):
    payload = json.loads(error.value.payload)
    assert payload["error"] == "stale_contact"
    assert WORDING in payload["instruction"] and str(ID) in payload["instruction"]
    assert "replace=True" in payload["instruction"]


@pytest.mark.asyncio
@pytest.mark.parametrize("getter", GETTERS)
async def test_alias_id_does_not_reach_marked_variants(getter):
    client = marked_peers()
    with pytest.raises(runtime.AliasNeedsUser) as error:
        await getter(runtime.AliasID(ID, WORDING), client)
    assert_stale(error)
    assert client.asked_marked() == []
    # the cold-cache retry on the exact id is kept
    assert client.calls == [ID, ID]
    assert client.dialogs_loaded == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("getter", GETTERS)
async def test_string_alias_does_not_reach_marked_variants(getter):
    client = marked_peers()
    with pytest.raises(runtime.AliasNeedsUser) as error:
        await getter(WORDING, client)
    assert_stale(error)
    assert client.calls == [ID, ID]


@pytest.mark.asyncio
@pytest.mark.parametrize("getter", GETTERS)
async def test_alias_id_that_resolves_directly_returns_it(getter):
    client = FakeClient({ID, CHANNEL})
    result = await getter(runtime.AliasID(ID, WORDING), client)
    assert result.endswith(f":{ID}")
    assert client.calls == [ID]


@pytest.mark.asyncio
@pytest.mark.parametrize("getter", GETTERS)
async def test_negative_alias_id_resolves_as_before(getter):
    client = FakeClient({CHANNEL})
    result = await getter(runtime.AliasID(CHANNEL, WORDING), client)
    assert result.endswith(f":{CHANNEL}")


@pytest.mark.asyncio
@pytest.mark.parametrize("getter", GETTERS)
@pytest.mark.parametrize("identifier", [ID, str(ID)], ids=["int", "digit-string"])
async def test_plain_positive_id_keeps_marked_fallback(getter, identifier):
    client = marked_peers()
    result = await getter(identifier, client)
    assert result.endswith(f":{CHANNEL}")
    assert client.calls == [ID, ID, CHANNEL]


@pytest.mark.asyncio
async def test_not_found_message_names_marked_variants_only_when_tried():
    client = FakeClient()
    with pytest.raises(runtime.EntityNotFoundError) as plain:
        await runtime._resolve_with_retries("get_entity", ID, client, "entity")
    assert str(plain.value) == (
        f"Could not resolve entity for {ID!r}, including marked variants [{CHANNEL}, {GROUP}]"
    )
    with pytest.raises(runtime.EntityNotFoundError) as exact:
        await runtime._resolve_with_retries("get_entity", ID, client, "entity", try_marked=False)
    assert str(exact.value) == f"Could not resolve entity for {ID!r}"


@pytest.mark.asyncio
async def test_allowlisted_alias_id_never_resolves_to_unlisted_marked_chat(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", str(ID))
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", None)
    for getter in GETTERS:
        client = marked_peers()
        with pytest.raises(runtime.AliasNeedsUser) as error:
            await getter(runtime.AliasID(ID, WORDING), client)
        assert_stale(error)
        assert client.asked_marked() == []
    assert runtime.is_chat_allowed(ID) and not runtime.is_chat_allowed(CHANNEL)


# --- through the real CallToolRequest handler -------------------------------------------


async def call(name, **arguments):
    request = CallToolRequest(
        method="tools/call", params=CallToolRequestParams(name=name, arguments=arguments)
    )
    return (await runtime.mcp._mcp_server.request_handlers[CallToolRequest](request)).root


def text(result):
    return "\n".join(b.text for b in result.content if b.type == "text")


@pytest.fixture
def probe():
    @runtime.mcp.tool(name="zz_task0010_probe")
    @runtime.with_account(readonly=True)
    @runtime.validate_id("chat_id", "others")
    async def tool(chat_id, others: list = None, account: str = None) -> str:
        try:
            entity = await runtime.resolve_entity(chat_id, runtime.get_client(account))
            return f"resolved {entity}"
        except Exception as error:
            return runtime.log_and_format_error("zz_task0010_probe", error)

    yield
    runtime.mcp._tool_manager._tools.pop("zz_task0010_probe", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True])
async def test_validate_id_tool_gets_stale_question(monkeypatch, probe, structured):
    if structured:
        monkeypatch.setenv("TELEGRAM_STRUCTURED_ERRORS", "1")
    client = marked_peers()
    monkeypatch.setattr(runtime, "clients", {"one": client})
    result = await call("zz_task0010_probe", chat_id=WORDING)
    assert json.loads(text(result))["error"] == "stale_contact"
    assert client.asked_marked() == []
    if structured:
        assert result.isError is True
        assert result.meta[META] == {"kind": "invalid_argument", "needs_user": True}
    else:
        assert result.meta is None


@pytest.mark.asyncio
async def test_validate_id_digit_string_and_int_keep_fallback(monkeypatch, probe):
    for value in (str(ID), ID):
        client = marked_peers()
        monkeypatch.setattr(runtime, "clients", {"one": client})
        result = await call("zz_task0010_probe", chat_id=value)
        assert text(result) == f"resolved get_entity:{CHANNEL}"


@pytest.mark.asyncio
async def test_fan_out_other_account_gets_stale_question_without_guessing(monkeypatch, probe):
    knows = FakeClient({ID})
    other = marked_peers()
    monkeypatch.setattr(runtime, "clients", {"a": knows, "b": other})
    result = await call("zz_task0010_probe", chat_id=WORDING)
    body = text(result)
    assert f"resolved get_entity:{ID}" in body
    assert "stale_contact" in body
    assert other.asked_marked() == []
    assert knows.calls == [ID]
