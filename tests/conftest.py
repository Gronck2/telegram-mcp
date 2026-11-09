"""Shared pytest setup for import-time Telegram configuration."""

import os

import pytest

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")
os.environ.setdefault("TELEGRAM_SESSION_NAME", "test_session")


@pytest.fixture
def transcript_cache_dir(tmp_path, monkeypatch):
    """Isolated, per-test SQLite transcript cache directory."""
    d = tmp_path / "transcripts"
    monkeypatch.setenv("TELEGRAM_TRANSCRIPT_CACHE_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def _clear_expected_username(monkeypatch):
    """Keep a developer's TELEGRAM_EXPECTED_USERNAME* (.env) away from fake clients."""
    for key in list(os.environ):
        if key.startswith("TELEGRAM_EXPECTED_USERNAME"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Fail loudly if a test tries to reach a non-loopback host (e.g. Telegram DCs).

    Tests must never hit real Telegram; a missing ``ensure_connected`` patch would
    otherwise only show up as a slow connection attempt. Loopback stays allowed.
    """
    import ipaddress
    import socket

    def _check(address):
        host = address[0] if isinstance(address, (tuple, list)) and address else address
        if isinstance(host, bytes):
            host = host.decode(errors="replace")
        try:
            if isinstance(host, str) and ipaddress.ip_address(host.split("%")[0]).is_loopback:
                return
        except ValueError:
            if host in ("localhost", "testserver"):
                return
        raise RuntimeError(f"Test attempted a real network connection to {address!r}")

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def connect(self, address):
        if self.family != getattr(socket, "AF_UNIX", None):
            _check(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        if self.family != getattr(socket, "AF_UNIX", None):
            _check(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
