# The agent's real MCP client: it talks to our own tool server and the borrowed
# mcp-server-time server over the MCP protocol (stdio), not by importing their code.
# Both subprocesses are opened once per agent run and kept open across every tool
# call in that run, then closed together at the end.
#
# The SIMULATED irrigation gateway (backend/irrigation_server) is registered here the
# same way, but in its own client, IrrigationGatewayClient, which only the approval
# executor (backend/approvals/irrigation_executor.py) opens after an officer approves.
# ToolClient - the agent's client - never connects to it, so no agent run can reach
# open_valve or close_valve.
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
    # AGRIMOIST_DB_READY: the app created the tables already, so the server skips its
    # own start-up checks (several database round trips on every agent run).
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "backend.mcp_server.server"],
        env={**os.environ, "AGRIMOIST_DB_READY": "1"},
    )


def _time_server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server_time", "--local-timezone", "Africa/Kampala"],
        env=os.environ.copy(),
    )


def _irrigation_server_params() -> StdioServerParameters:
    # First-party, like our AgriMoist server: same environment (DATABASE_URL in particular).
    return StdioServerParameters(
        command=sys.executable, args=["-m", "backend.irrigation_server.server"], env=os.environ.copy()
    )


async def _open_session(stack: AsyncExitStack, params: StdioServerParameters) -> tuple[ClientSession, str]:
    read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    init = await session.initialize()
    return session, init.serverInfo.name


async def _list_tools(session: ClientSession) -> list[dict]:
    listed = await session.list_tools()
    return [
        {
            "name": tool.name,
            "description": (tool.description or "").strip(),
            "inputs": sorted((tool.inputSchema or {}).get("properties", {})),
        }
        for tool in listed.tools
    ]


class ToolClient:
    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self.agrimoist: ClientSession | None = None
        self.time: ClientSession | None = None
        self._server_names: dict[int, str] = {}

    async def __aenter__(self) -> "ToolClient":
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        self.agrimoist = await self._open(_agrimoist_server_params())
        self.time = await self._open(_time_server_params())
        return self

    async def _open(self, params: StdioServerParameters) -> ClientSession:
        assert self._stack is not None
        session, name = await _open_session(self._stack, params)
        self._server_names[id(session)] = name
        return session

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(*exc_info)

    async def call(self, tool_name: str, **arguments: Any) -> dict:
        assert self.agrimoist is not None
        result = await self.agrimoist.call_tool(tool_name, arguments)
        return _parse(result, tool_name)

    async def list_servers(self) -> list[dict]:
        """Ask each connected server, over MCP, who it is and which tools it offers.
        Used by the officer "AI tools (MCP)" screen and GET /api/mcp."""
        servers = []
        for kind, session in (("Built by us", self.agrimoist), ("Borrowed", self.time)):
            assert session is not None
            servers.append({"name": self._server_names[id(session)], "kind": kind, "tools": await _list_tools(session)})
        return servers

    async def now(self, timezone: str = "Africa/Kampala") -> dict:
        assert self.time is not None
        result = await self.time.call_tool("get_current_time", {"timezone": timezone})
        return _parse(result, "get_current_time")


class IrrigationGatewayClient:
    """The SIMULATED irrigation gateway over MCP (stdio). Used only by the approval
    executor, after approval - never by the agent (see the note at the top)."""

    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self.gateway: ClientSession | None = None
        self.name: str | None = None

    async def __aenter__(self) -> "IrrigationGatewayClient":
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        try:
            self.gateway, self.name = await _open_session(self._stack, _irrigation_server_params())
        except BaseException:
            await self._stack.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(*exc_info)

    async def call(self, tool_name: str, **arguments: Any) -> dict:
        assert self.gateway is not None
        return _parse(await self.gateway.call_tool(tool_name, arguments), tool_name)

    async def describe(self) -> dict:
        assert self.gateway is not None
        return {"name": self.name, "kind": "Simulated device", "tools": await _list_tools(self.gateway)}


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


# ---------------------------------------------------------------------------
# Warm tool servers. Starting the two MCP servers (two Python processes) costs several
# seconds, so the web app keeps one ToolClient open between agent runs instead of
# starting fresh each time. One background task owns it (the MCP stdio client must be
# opened and closed in the same task); runs only send requests through it. It is
# restarted when the settings the servers read change (DATABASE_URL, WEATHER_FAIL, ...)
# or when a server dies. Tests turn this off (KEEP_TOOL_SERVERS_WARM=false).
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402

_ENV_KEYS = (
    "DATABASE_URL", "WEATHER_FAIL", "WEATHER_OVERRIDE_PAST_MM", "WEATHER_OVERRIDE_NEXT_MM",
    "SOIL_SENSOR_FAIL", "SOIL_SOURCE", "HEAVY_RAIN_THRESHOLD_MM", "GROQ_API_KEY", "SUNBIRD_API_KEY",
    "IRRIGATION_DRY_THRESHOLD_PCT", "IRRIGATION_MAX_MINUTES", "DRAFT_EXPIRY_HOURS",
)


class _WarmClient:
    def __init__(self) -> None:
        self.client: ToolClient | None = None
        self.key: tuple | None = None
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _current_key(self) -> tuple:
        return (id(asyncio.get_running_loop()),) + tuple(os.environ.get(k) for k in _ENV_KEYS)

    async def _own(self, ready: asyncio.Future) -> None:
        try:
            async with ToolClient() as client:
                self.client = client
                ready.set_result(client)
                await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001
            if not ready.done():
                ready.set_exception(exc)
        finally:
            self.client = None

    async def get(self) -> ToolClient:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:
            self._lock, self._loop = asyncio.Lock(), loop
            self.client, self._task, self.key = None, None, None
        async with self._lock:
            key = self._current_key()
            alive = self._task is not None and not self._task.done() and self.client is not None
            if alive and key == self.key:
                return self.client
            await self.close()
            self._stop = asyncio.Event()
            ready: asyncio.Future = loop.create_future()
            self._task = loop.create_task(self._own(ready))
            client = await ready
            self.key = key
            return client

    async def close(self) -> None:
        if self._task is not None and not self._task.done() and self._stop is not None:
            self._stop.set()
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except Exception:  # noqa: BLE001 - closing is best effort
                self._task.cancel()
        self._task, self.client = None, None

    def reset(self) -> None:
        """Forget a client whose server died; the next get() starts a fresh one."""
        if self._stop is not None:
            self._stop.set()
        self.key = None


warm_tools = _WarmClient()
