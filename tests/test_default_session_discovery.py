"""Account discovery for the optional default file session, and audit log location.

Real Telethon SQLiteSession files are created only under tmp_path.
"""

import json
import os
import sqlite3

import pytest
from telethon.sessions import SQLiteSession

from telegram_mcp import audit
from telegram_mcp import runtime as runtime_module


@pytest.fixture
def discovery(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("TELEGRAM_SESSION_STRING", "TELEGRAM_SESSION_NAME")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(runtime_module, "OPTIONAL_ACCOUNTS", set())
    monkeypatch.setattr(runtime_module, "StringSession", lambda value=None: "str::" + str(value))
    monkeypatch.setattr(
        runtime_module,
        "TelegramClient",
        lambda session, *a, **k: SQLiteSession(session) if session.startswith("/") else object(),
        raising=False,
    )
    return monkeypatch


def test_writable_default_file_session_next_to_labelled_account(discovery, tmp_path):
    discovery.setenv("TELEGRAM_SESSION_STRING_WORK", "w")
    discovery.setenv("TELEGRAM_SESSION_NAME", str(tmp_path / "def"))

    accounts = runtime_module._discover_accounts()

    assert set(accounts) == {"work", "default"}
    assert runtime_module.OPTIONAL_ACCOUNTS == {"default"}
    accounts["default"].close()


def test_labelled_only_has_no_default_and_no_optional(discovery):
    discovery.setenv("TELEGRAM_SESSION_STRING_WORK", "w")

    assert set(runtime_module._discover_accounts()) == {"work"}
    assert runtime_module.OPTIONAL_ACCOUNTS == set()


def test_unsuffixed_string_next_to_labelled_is_required_default(discovery):
    discovery.setenv("TELEGRAM_SESSION_STRING_WORK", "w")
    discovery.setenv("TELEGRAM_SESSION_STRING", "d")

    assert set(runtime_module._discover_accounts()) == {"work", "default"}
    assert runtime_module.OPTIONAL_ACCOUNTS == set()


def test_no_credentials_exits(discovery):
    with pytest.raises(SystemExit):
        runtime_module._discover_accounts()


def test_corrupt_default_session_file_fails_loudly_not_silently(discovery, tmp_path):
    """A corrupt file is not 'unwritable': startup refuses rather than dropping it (as on base)."""
    (tmp_path / "bad.session").write_text("not a database")
    discovery.setenv("TELEGRAM_SESSION_STRING_WORK", "w")
    discovery.setenv("TELEGRAM_SESSION_NAME", str(tmp_path / "bad"))

    with pytest.raises(sqlite3.DatabaseError):
        runtime_module._discover_accounts()


def test_locked_default_session_file_is_skipped_with_warning(discovery, tmp_path, caplog):
    """A locked file raises OperationalError like an unwritable one: skipped, with a warning."""
    path = tmp_path / "locked.session"
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("create table t(a)")
    holder.execute("begin exclusive")
    discovery.setenv("TELEGRAM_SESSION_STRING_WORK", "w")
    discovery.setenv("TELEGRAM_SESSION_NAME", str(tmp_path / "locked"))
    try:
        with caplog.at_level("WARNING", logger="telegram_mcp"):
            accounts = runtime_module._discover_accounts()
    finally:
        holder.close()

    assert set(accounts) == {"work"}
    assert any("optional default file session" in r.getMessage() for r in caplog.records)
    assert str(tmp_path) not in caplog.text


def test_locked_required_default_session_still_errors(discovery, tmp_path):
    path = tmp_path / "locked.session"
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("create table t(a)")
    holder.execute("begin exclusive")
    discovery.setenv("TELEGRAM_SESSION_NAME", str(tmp_path / "locked"))
    try:
        with pytest.raises(sqlite3.OperationalError):
            runtime_module._discover_accounts()
    finally:
        holder.close()


def test_audit_event_lands_in_env_log(monkeypatch, tmp_path):
    log = tmp_path / "state" / "audit.jsonl"
    monkeypatch.setenv("TELEGRAM_AUDIT_LOG", str(log))

    audit.record_audit_event("delete_message", 1, "ok", account="work", message_ids=[2])

    record = json.loads(log.read_text().splitlines()[0])
    assert record["tool"] == "delete_message" and record["account"] == "work"


def test_audit_default_path_unchanged_when_unset(monkeypatch):
    monkeypatch.delenv("TELEGRAM_AUDIT_LOG", raising=False)
    assert audit.get_audit_log_path() == audit.PROJECT_ROOT / "telegram_audit.log"
    monkeypatch.setenv("TELEGRAM_AUDIT_LOG", "   ")
    assert audit.get_audit_log_path() == audit.PROJECT_ROOT / "telegram_audit.log"
