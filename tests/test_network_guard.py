"""The autouse ``_no_real_network`` fixture must block non-loopback connects."""

import asyncio
import socket

import pytest

TG_DC = ("149.154.167.51", 443)


def _expect_recorded(blocked):
    """A blocked connect is recorded (teardown fails otherwise); this test expects it."""
    assert blocked
    blocked.clear()


def test_blocking_connect_is_blocked(_no_real_network):
    with pytest.raises(RuntimeError, match="real network"):
        socket.create_connection(TG_DC, timeout=1)
    _expect_recorded(_no_real_network)


def test_connect_ex_is_blocked(_no_real_network):
    s = socket.socket()
    try:
        with pytest.raises(RuntimeError, match="real network"):
            s.connect_ex(TG_DC)
    finally:
        s.close()
    _expect_recorded(_no_real_network)


def test_loopback_is_not_blocked_by_guard():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        c = socket.create_connection(srv.getsockname(), timeout=1)
        c.close()
    finally:
        srv.close()


def test_asyncio_sock_connect_is_blocked(_no_real_network):
    async def go():
        s = socket.socket()
        s.setblocking(False)
        try:
            await asyncio.get_running_loop().sock_connect(s, TG_DC)
        finally:
            s.close()

    with pytest.raises(RuntimeError, match="real network"):
        asyncio.run(go())
    _expect_recorded(_no_real_network)


def test_asyncio_open_connection_is_blocked(_no_real_network):
    async def go():
        await asyncio.open_connection(*TG_DC)

    with pytest.raises(RuntimeError, match="real network"):
        asyncio.run(go())
    _expect_recorded(_no_real_network)


def test_telethon_connect_is_blocked(_no_real_network):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    async def go():
        client = TelegramClient(StringSession(), 12345, "dummy_hash", connection_retries=1)
        try:
            await asyncio.wait_for(client.connect(), 10)
        finally:
            client.disconnect()

    with pytest.raises(RuntimeError, match="real network"):
        asyncio.run(go())
    _expect_recorded(_no_real_network)
