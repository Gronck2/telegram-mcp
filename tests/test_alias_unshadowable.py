"""Aliases cannot shadow any string Telethon reads as a username (task 0005)."""

import json

import pytest

from telegram_mcp import runtime
from telegram_mcp.tools import contacts

X = -1001234567890
UNICODE_NAME = "aдминz"


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


def _legacy_aliases(tmp_path, *keys):
    path = tmp_path / "aliases.json"
    path.write_text(
        json.dumps({k: {"id": X, "name": None, "account": None} for k in keys}),
        encoding="utf-8",
    )


@pytest.mark.parametrize("name", ["abcd", UNICODE_NAME])
def test_username_grant_is_never_redirected_by_an_alias(monkeypatch, tmp_path, name):
    _legacy_aliases(tmp_path, name, "лена", "my friend")
    monkeypatch.setenv("TELEGRAM_ALLOWED_WRITE_CHATS", f"{name},{X}")
    monkeypatch.setenv("TELEGRAM_ALLOWED_READ_CHATS", str(X))

    assert runtime.apply_alias(name) == name
    assert runtime.apply_alias("лена") == X
    assert runtime.apply_alias("my friend") == X


@pytest.mark.parametrize("name", ["abcd", UNICODE_NAME])
def test_legacy_alias_still_resolves_without_allowlist(tmp_path, name):
    _legacy_aliases(tmp_path, name)
    assert runtime.apply_alias(name) == X


@pytest.mark.parametrize("name", ["abcd", UNICODE_NAME])
def test_grant_needs_unshadowable_string(monkeypatch, name):
    allowed = {name.lower()}
    assert runtime._identifier_matches_allowlist(allowed, name)
    assert runtime.is_unshadowable_identifier(name)
    assert not runtime.is_unshadowable_identifier("my friend")
    assert not runtime.is_unshadowable_identifier("лена")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["abcd", UNICODE_NAME, "@abcd"])
async def test_set_contact_alias_refuses_username_shaped_key(monkeypatch, name):
    out = await contacts.set_contact_alias(name, "101")
    assert "alias_shadows_real_identifier" in out
    assert runtime.load_aliases() == {}


@pytest.mark.asyncio
async def test_forward_send_as_never_acts_as_alias_target(monkeypatch, tmp_path):
    """Write entry 'abcd' + read-only X + alias abcd->X: send_as stays the real @abcd."""
    _legacy_aliases(tmp_path, "abcd")
    monkeypatch.setenv("TELEGRAM_ALLOWED_WRITE_CHATS", "abcd")
    monkeypatch.setenv("TELEGRAM_ALLOWED_READ_CHATS", str(X))
    seen = []

    class Cl:
        async def get_entity(self, ident):
            seen.append(ident)
            raise ValueError("no such user")

    async def connected(c=None):
        return None

    monkeypatch.setattr(runtime, "get_client", lambda account=None: Cl())
    monkeypatch.setattr(runtime, "ensure_connected", connected)

    await runtime._peer_item_allowed("abcd", runtime.CHAT_ACCESS_WRITE, None)
    assert X not in seen


# ---------------------------------------------------------------------------
# End to end through the tools: a legacy alias never redirects a username grant
# ---------------------------------------------------------------------------

from telethon import types  # noqa: E402

from telegram_mcp.tools import messages, profile  # noqa: E402

REAL_ABCD = types.User(id=555, first_name="Real", username="abcd", access_hash=5, bot=True)
SECRET_X = types.Channel(
    id=1234567890, title="SECRET-X", photo=None, date=None, access_hash=7, megagroup=True
)
IVAN = types.User(id=101, first_name="Ivan", username="ivan_k", access_hash=1)
DEST = types.Channel(id=1002, title="Dest", photo=None, date=None, access_hash=8, megagroup=True)


class LookupClient:
    """Records every lookup; 'abcd' is a real user, X is a hidden channel."""

    def __init__(self):
        self.lookups = []
        self.requests = []

    def _find(self, ident):
        self.lookups.append(ident)
        if ident in ("abcd", "@abcd"):
            return REAL_ABCD
        if ident == X:
            return SECRET_X
        if ident in (101, "101"):
            return IVAN
        if ident in (-1002, -1000000001002):
            return DEST
        raise ValueError("no such peer")

    async def get_entity(self, ident):
        return self._find(ident)

    async def get_input_entity(self, ident):
        return types.InputPeerUser(user_id=self._find(ident).id, access_hash=5)

    async def get_dialogs(self):
        return []

    async def get_messages(self, entity, ids=None):
        return None  # not part of an album

    async def __call__(self, request):
        self.requests.append(request)
        return types.Updates(updates=[], users=[], chats=[], date=None, seq=0)


@pytest.fixture
def shadowed(monkeypatch, tmp_path):
    """Write entry 'abcd', read-only X, legacy alias abcd -> X."""
    _legacy_aliases(tmp_path, "abcd")
    monkeypatch.setenv("TELEGRAM_ALLOWED_WRITE_CHATS", "abcd,-1002")
    monkeypatch.setenv("TELEGRAM_ALLOWED_READ_CHATS", str(X))
    cl = LookupClient()

    async def connected(c=None):
        return None

    for module in (runtime, contacts, messages, profile):
        monkeypatch.setattr(module, "get_client", lambda account=None: cl)
        monkeypatch.setattr(module, "ensure_connected", connected)
    return cl


@pytest.mark.asyncio
async def test_forward_message_send_as_abcd_never_acts_as_x(shadowed):
    out = await messages.forward_message(
        from_chat_id=X, message_id=1, to_chat_id=-1002, send_as="abcd"
    )

    assert "error occurred" not in out, out
    [request] = [r for r in shadowed.requests if type(r).__name__ == "ForwardMessagesRequest"]
    assert request.send_as.user_id == REAL_ABCD.id
    assert shadowed.lookups.count(X) <= 1  # only the from_chat_id lookup


@pytest.mark.asyncio
async def test_set_contact_alias_chat_id_abcd_never_binds_x(shadowed):
    out = await contacts.set_contact_alias("new nick", "abcd")

    saved = runtime.load_aliases()
    assert saved.get("new nick", {}).get("id") != X, out
    assert saved["abcd"]["id"] == X  # the legacy record is untouched
    assert "SECRET-X" not in out


@pytest.mark.asyncio
async def test_get_bot_info_abcd_never_reads_x(shadowed):
    out = await profile.get_bot_info(bot_username="abcd")

    assert "SECRET-X" not in out
    assert X not in shadowed.lookups
    assert [r for r in shadowed.requests if type(r).__name__ == "GetFullUserRequest"] == []


# ---------------------------------------------------------------------------
# Regression: ordinary nicknames keep working, with and without an allowlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("nickname", ["лена", "my friend", "Mom 2"])
@pytest.mark.parametrize("allowlist", [False, True])
async def test_ordinary_nickname_saves_and_resolves(monkeypatch, shadowed, nickname, allowlist):
    if allowlist:
        monkeypatch.setenv("TELEGRAM_ALLOWED_WRITE_CHATS", "101")
    else:
        for name in ("TELEGRAM_ALLOWED_WRITE_CHATS", "TELEGRAM_ALLOWED_READ_CHATS"):
            monkeypatch.delenv(name)

    saved = await contacts.set_contact_alias(nickname, "101")

    assert '"saved":true' in saved.replace(" ", "").lower(), saved
    assert runtime.apply_alias(nickname) == 101
    assert runtime.apply_alias(nickname.upper()) == 101


@pytest.mark.asyncio
async def test_set_contact_alias_username_shaped_key_gives_clear_error():
    out = json.loads(await contacts.set_contact_alias("bob", "101"))["results"]

    assert out["saved"] is False
    assert out["reason"] == "alias_shadows_real_identifier"
    assert "distinct nickname" in out["detail"]


# ---------------------------------------------------------------------------
# Network guard: a swallowed connect still fails the test (tests/conftest.py)
# ---------------------------------------------------------------------------

pytest_plugins = ["pytester"]


def test_swallowed_blocked_connect_fails_the_test(pytester):
    pytester.makeconftest(
        (pytest.importorskip("pathlib").Path(__file__).parent / "conftest.py").read_text()
    )
    pytester.makepyfile("""
        import socket

        def test_swallows():
            s = socket.socket()
            try:
                s.connect(("149.154.167.51", 443))
            except Exception:
                pass  # production code's broad except
            finally:
                s.close()

        def test_loopback_is_fine():
            s = socket.socket()
            try:
                s.connect_ex(("127.0.0.1", 9))
            finally:
                s.close()
        """)
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(["*tried real network connects*149.154.167.51*"])
