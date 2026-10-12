import asyncio
from urllib.parse import urlsplit

import pytest
import lumabox.proxy as proxy_module
from lumabox.proxy import PublicProxy


async def connect_proxy(proxy, payload):
    address = urlsplit(proxy.server_url)
    reader, writer = await asyncio.open_connection(address.hostname, address.port)
    writer.write(payload)
    await writer.drain()
    return reader, writer


@pytest.mark.parametrize("target", [b"127.0.0.1:443", b"p3.douyinpic.com:80", b"u@p3.douyinpic.com:443", b"[::1]:443"])
async def test_proxy_rejects_unsafe_authority(target):
    async with PublicProxy() as proxy:
        reader, writer = await connect_proxy(proxy, b"CONNECT " + target + b" HTTP/1.1\r\n\r\n")
        assert (await reader.readline()).startswith(b"HTTP/1.1 502")
        writer.close()
        await writer.wait_closed()


async def test_proxy_relays_bytes_and_closes_tunnel(monkeypatch):
    async def echo(reader, writer):
        writer.write(await reader.read(64))
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async def dial(ip):
        assert ip == "93.184.216.34"
        return await asyncio.open_connection("127.0.0.1", port)
    monkeypatch.setattr(proxy_module, "resolve_public", lambda host: ("93.184.216.34",))
    monkeypatch.setattr(proxy_module, "_dial", dial)
    try:
        async with PublicProxy() as proxy:
            reader, writer = await connect_proxy(proxy, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
            assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
            writer.write(b"fixture payload")
            await writer.drain()
            assert await reader.read(64) == b"fixture payload"
            writer.close()
            await writer.wait_closed()
        assert not proxy.tasks
    finally:
        server.close()
        await server.wait_closed()
