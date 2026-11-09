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
