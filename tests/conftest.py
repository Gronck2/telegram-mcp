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


def _assert_loopback(address):
    """Raise unless ``address`` (host, port, ...) names a loopback host."""
    import ipaddress

    host = address[0] if isinstance(address, (tuple, list)) and address else address
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    try:
        if ipaddress.ip_address(str(host).split("%")[0]).is_loopback:
            return
    except ValueError:
        if host in ("localhost", "testserver"):
            return
    raise RuntimeError(f"Test attempted a real network connection to {address!r}")


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Fail loudly if a test tries to reach a non-loopback host (e.g. Telegram DCs).

    Tests must never hit real Telegram; a missing ``ensure_connected`` patch would
    otherwise only show up as a slow connection attempt. Loopback stays allowed.
    """
    import socket

    def guarded(real):
        def wrapper(self, address):
            if self.family != getattr(socket, "AF_UNIX", None):
                _assert_loopback(address)
            return real(self, address)

        return wrapper

    monkeypatch.setattr(socket.socket, "connect", guarded(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(socket.socket.connect_ex))
