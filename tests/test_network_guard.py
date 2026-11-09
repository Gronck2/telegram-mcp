"""The autouse ``_no_real_network`` fixture must block non-loopback connects."""

import asyncio
import socket

import pytest

TG_DC = ("149.154.167.51", 443)


def test_blocking_connect_is_blocked():
    with pytest.raises(RuntimeError, match="real network"):
        socket.create_connection(TG_DC, timeout=1)


def test_connect_ex_is_blocked():
    s = socket.socket()
    try:
        with pytest.raises(RuntimeError, match="real network"):
            s.connect_ex(TG_DC)
    finally:
        s.close()


def test_loopback_is_not_blocked_by_guard():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        c = socket.create_connection(srv.getsockname(), timeout=1)
        c.close()
    finally:
        srv.close()


def test_asyncio_sock_connect_is_blocked():
    async def go():
        s = socket.socket()
        s.setblocking(False)
        try:
            await asyncio.get_running_loop().sock_connect(s, TG_DC)
        finally:
            s.close()

    with pytest.raises(RuntimeError, match="real network"):
        asyncio.run(go())


def test_asyncio_open_connection_is_blocked():
    async def go():
        await asyncio.open_connection(*TG_DC)

    with pytest.raises(RuntimeError, match="real network"):
        asyncio.run(go())


def test_telethon_connect_is_blocked():
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
