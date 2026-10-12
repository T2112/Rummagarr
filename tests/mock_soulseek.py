"""A local Soulseek server that answers a search with a real peer reply."""

from __future__ import annotations

import asyncio
import struct

from aioslsk.protocol.messages import FileSearch, Login, PeerInit, PeerSearchReply, ServerMessage, ServerSearchRequest
from aioslsk.protocol.obfuscation import encode
from aioslsk.protocol.primitives import Attribute, FileData


def sample_reply(ticket: int) -> PeerSearchReply.Request:
    shared = FileData(
        unknown=1,
        filename=r"music\Ella Langley\Hungover\01 Blue.flac",
        filesize=30_000_000,
        extension="flac",
        attributes=[Attribute(0, 900), Attribute(1, 214)],
    )
    locked = FileData(
        unknown=1,
        filename=r"music\Ella Langley\Hungover\locked.mp3",
        filesize=8_000_000,
        extension="mp3",
        attributes=[Attribute(0, 320), Attribute(1, 200)],
    )
    return PeerSearchReply.Request(
        username="vinylqueen",
        ticket=ticket,
        results=[shared],
        has_slots_free=True,
        avg_speed=250_000,
        queue_size=0,
        unknown=0,
        locked_results=[locked],
    )


async def _read_frame(reader: asyncio.StreamReader) -> bytes:
    header = await reader.readexactly(4)
    (length,) = struct.unpack("<I", header)
    body = await reader.readexactly(length)
    return header + body


class MockSoulseek:
    """Speaks the server frames aioslsk sends, then connects back with PeerSearchReply."""

    def __init__(self) -> None:
        self.listen_port = 0
        self.obfuscated_port = 0
        self.mode = "clear"
        self.searches: list[tuple[int, str]] = []
        # When set, the server pushes one distributed search right after login.
        # A real server does this continuously once the client is a branch root.
        self.push_server_search = False
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                raw = await _read_frame(reader)
                message = ServerMessage.deserialize_request(raw)
                if isinstance(message, Login.Request):
                    writer.write(
                        Login.Response(
                            success=True,
                            greeting="hi",
                            ip="127.0.0.1",
                            md5hash="abc",
                            privileged=False,
                        ).serialize()
                    )
                    await writer.drain()
                    if self.push_server_search:
                        # Another user's search, delivered the way the server
                        # delivers them to a distributed branch root.
                        writer.write(
                            ServerSearchRequest.Response(
                                distributed_code=3,
                                unknown=0,
                                username="browser",
                                ticket=99,
                                query="mp3",
                            ).serialize()
                        )
                        await writer.drain()
                        self.push_server_search = False
                elif isinstance(message, FileSearch.Request):
                    self.searches.append((message.ticket, message.query))
                    asyncio.create_task(self._reply(message.ticket))
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _reply(self, ticket: int) -> None:
        await asyncio.sleep(0.05)
        port = self.obfuscated_port if self.mode == "obfuscated" else self.listen_port
        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        init = PeerInit.Request(username="vinylqueen", typ="P", ticket=0).serialize()
        reply = sample_reply(ticket).serialize()
        if self.mode == "obfuscated":
            init = encode(init)
            reply = encode(reply)
        try:
            writer.write(init)
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.write(reply)
            await writer.drain()
            await asyncio.sleep(0.3)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
