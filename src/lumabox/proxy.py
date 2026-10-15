"""A temporary CONNECT relay. TLS remains end-to-end with Chromium."""

import asyncio
from urllib.parse import urlsplit

from .errors import LumaBoxError
from .net import resolve_public
from .diagnostics import network_reason, public_diagnostics


async def _dial(ip: str):
    return await asyncio.wait_for(asyncio.open_connection(ip, 443), 15)


class PublicProxy:
    def __init__(self):
        self.tasks: set[asyncio.Task] = set()
        self.connections: set = set()
        self.failures: list[dict] = []
        self.server = None

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=16384)
        self.server_url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *args):
        self.server.close()
        await self.server.wait_closed()
        for connection in tuple(self.connections):
            connection.close()
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        self.tasks.clear()
        self.connections.clear()

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        self.connections.add(writer)
        upstream = None
        pumps = []
        host = None
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
            method, authority, version = header.split(b"\r\n", 1)[0].decode("ascii").split()
            parts = urlsplit("https://" + authority)
            host = parts.hostname
            if method != "CONNECT" or version not in ("HTTP/1.0", "HTTP/1.1") or not host or parts.port != 443 or parts.username is not None or parts.path or parts.query or parts.fragment or "%" in authority or "\\" in authority:
                raise LumaBoxError("UNSAFE_TARGET", "Invalid CONNECT target")
            try:
                addresses = await asyncio.wait_for(asyncio.to_thread(resolve_public, host), 15)
            except TimeoutError:
                raise LumaBoxError("RESOURCE_TIMEOUT", "DNS resolution exceeded its time limit", diagnostics={
                    "stage": "dns_resolution", "host": host, "network_reason": "DNS_TIMEOUT"}) from None
            remote_reader, upstream = await _dial(addresses[0])
            self.connections.add(upstream)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()

            async def pump(source, destination):
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()

            pumps = [asyncio.create_task(pump(reader, upstream)), asyncio.create_task(pump(remote_reader, writer))]
            completed, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for pump_task in completed:
                pump_task.result()  # Preserve network failures before gathering tasks during teardown.
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, LumaBoxError) else "DOWNLOAD_FAILED"
            if len(self.failures) < 100:
                detail = public_diagnostics(exc.diagnostics) if isinstance(exc, LumaBoxError) else {
                    "network_reason": network_reason(exc) or "CONNECTION_FAILED"}
                self.failures.append({**detail, "host": host, "code": code})
            if upstream is None:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                try:
                    await writer.drain()
                except OSError:
                    pass
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for connection in (upstream, writer):
                if connection is not None:
                    connection.close()
                    self.connections.discard(connection)
                    try:
                        await connection.wait_closed()
                    except OSError:
                        pass
            self.tasks.discard(task)
