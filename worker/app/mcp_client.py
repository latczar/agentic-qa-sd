"""Calling the MCP server's tools from the worker.

The worker is synchronous (pika's BlockingConnection), the MCP SDK is async,
so each call opens a session, runs, and closes inside one asyncio.run(). That
is more connection setup than a long-lived session would need, but a ticket
takes ~15s of model time anyway - a few milliseconds of handshake is not the
bottleneck, and it keeps the sync worker free of an event loop it would
otherwise have to manage.
"""

import asyncio
import json
import logging
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from shared.config import MCP_SERVER_URL

logger = logging.getLogger("worker.mcp")


class MCPError(RuntimeError):
    """The MCP server was unreachable or the tool call failed."""


async def _call(tool: str, arguments: dict[str, Any]) -> list[Any]:
    async with streamablehttp_client(MCP_SERVER_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)

    if result.isError:
        raise MCPError(f"tool '{tool}' failed: {result.content}")

    # A tool returning a list of N items comes back as N separate content
    # items, not one item holding a list - so always collect them all and let
    # the caller decide whether it wanted one object or many.
    return [json.loads(item.text) for item in result.content if getattr(item, "text", None)]


def call_tool(tool: str, arguments: dict[str, Any]) -> list[Any]:
    """Call a tool and return every content item it produced."""
    try:
        return asyncio.run(_call(tool, arguments))
    except MCPError:
        raise
    except Exception as exc:  # connection refused, timeout, protocol error
        raise MCPError(f"could not call MCP tool '{tool}': {exc}") from exc


def call_tool_single(tool: str, arguments: dict[str, Any]) -> Any:
    """For tools that return one object rather than a list."""
    payloads = call_tool(tool, arguments)
    return payloads[0] if payloads else None


def search_knowledge_base(query: str, limit: int = 3) -> list[dict]:
    rows = call_tool("search_knowledge_base", {"query": query, "limit": limit})

    # The tool reports its own failures as a single {"error": ...} entry rather
    # than raising across the protocol boundary, so unpack that back into an
    # exception the orchestrator already knows how to retry.
    if len(rows) == 1 and isinstance(rows[0], dict) and "error" in rows[0]:
        raise MCPError(rows[0]["error"])

    return rows
