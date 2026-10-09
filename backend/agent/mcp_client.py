# The agent's real MCP client: it talks to our own tool server and the borrowed
# mcp-server-time server over the MCP protocol (stdio), not by importing their code.
# Both subprocesses are opened once per agent run and kept open across every tool
# call in that run, then closed together at the end.
from __future__ import annotations

import json
import os
import sys
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from backend.tools.errors import ToolError


def _agrimoist_server_params() -> StdioServerParameters:
    # The MCP SDK only forwards a small, safe subset of environment variables to a
    # spawned server by default (it has to assume servers in a config file are
    # untrusted). Our own server is first-party code we just wrote, so it needs the
    # same environment as the parent process - in particular DATABASE_URL (so it
    # reads the same SQLite file), GROQ_API_KEY and WEATHER_FAIL. Built fresh on
    # every call (not cached at import time) so it always reflects the current
    # environment - important for tests, which point DATABASE_URL at a temp file
    # per test, and for WEATHER_FAIL being toggled without a server restart.
    return StdioServerParameters(
        command=sys.executable, args=["-m", "backend.mcp_server.server"], env=os.environ.copy()
    )


def _time_server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server_time", "--local-timezone", "Africa/Kampala"],
        env=os.environ.copy(),
    )


class ToolClient:
    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self.agrimoist: ClientSession | None = None
        self.time: ClientSession | None = None

    async def __aenter__(self) -> "ToolClient":
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        self.agrimoist = await self._open(_agrimoist_server_params())
        self.time = await self._open(_time_server_params())
        return self

    async def _open(self, params: StdioServerParameters) -> ClientSession:
        assert self._stack is not None
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(*exc_info)

    async def call(self, tool_name: str, **arguments: Any) -> dict:
        assert self.agrimoist is not None
        result = await self.agrimoist.call_tool(tool_name, arguments)
        return _parse(result, tool_name)

    async def now(self, timezone: str = "Africa/Kampala") -> dict:
        assert self.time is not None
        result = await self.time.call_tool("get_current_time", {"timezone": timezone})
        return _parse(result, "get_current_time")


def _parse(result: Any, tool_name: str) -> dict:
    if getattr(result, "structuredContent", None) is not None:
        return result.structuredContent
    text = result.content[0].text if result.content else ""
    if result.isError:
        raise ToolError(f"{tool_name} failed: {text}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}
