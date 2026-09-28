"""Authenticated JSON-RPC client for the independent Fridge Chef MCP service."""

from __future__ import annotations

import itertools
from typing import Any, Dict, List, Mapping, Optional, Sequence

import httpx

JsonObject = Dict[str, Any]


class MCPClientError(RuntimeError):
    """Raised when the MCP transport or JSON-RPC contract fails."""


class MCPClient:
    """Calls the MCP server over HTTP without importing its implementation."""

    def __init__(
        self,
        server_url: str,
        access_token: str,
        *,
        timeout: float = 30.0,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        if not server_url.strip():
            raise ValueError("server_url is required")
        if not access_token:
            raise ValueError("access_token is required")

        base_url = server_url.rstrip("/")
        self._endpoint = base_url if base_url.endswith("/mcp") else f"{base_url}/mcp"
        self._access_token = access_token
        self._request_ids = itertools.count(1)
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        """Close the HTTP client when this instance created it."""
        if self._owns_http_client:
            await self._http_client.aclose()

    async def list_tools(self) -> List[JsonObject]:
        """Fetch and validate the MCP server's authoritative tool definitions."""
        result = await self._rpc("tools/list", {}, conversation_id="startup")
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise MCPClientError("MCP tools/list result did not contain a tool list")

        validated: List[JsonObject] = []
        for tool in tools:
            if not isinstance(tool, dict):
                raise MCPClientError("MCP tools/list returned a non-object tool")
            name = tool.get("name")
            description = tool.get("description")
            input_schema = tool.get("inputSchema")
            if not isinstance(name, str) or not name:
                raise MCPClientError("MCP tool is missing a valid name")
            if not isinstance(description, str) or not description:
                raise MCPClientError(f"MCP tool '{name}' is missing a description")
            if not isinstance(input_schema, dict):
                raise MCPClientError(f"MCP tool '{name}' is missing an input schema")
            validated.append(tool)
        return validated

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        conversation_id: str,
    ) -> JsonObject:
        """Call one MCP tool and return its shared structured envelope."""
        result = await self._rpc(
            "tools/call",
            {"name": name, "arguments": dict(arguments)},
            conversation_id=conversation_id,
        )
        structured_content = result.get("structuredContent")
        if not isinstance(structured_content, dict):
            raise MCPClientError(
                f"MCP tool '{name}' did not return structuredContent"
            )
        if structured_content.get("status") not in {
            "success",
            "user_fixable",
            "system_error",
        }:
            raise MCPClientError(f"MCP tool '{name}' returned an invalid status")
        return structured_content

    @staticmethod
    def to_azure_tools(tools: Sequence[Mapping[str, Any]]) -> List[JsonObject]:
        """Translate discovered MCP schemas into Azure Responses function tools."""
        translated: List[JsonObject] = []
        for tool in tools:
            name = tool.get("name")
            description = tool.get("description")
            input_schema = tool.get("inputSchema")
            if not isinstance(name, str) or not isinstance(description, str):
                raise MCPClientError("Cannot translate an invalid MCP tool")
            if not isinstance(input_schema, dict):
                raise MCPClientError(f"MCP tool '{name}' has no input schema")
            translated.append(
                {
                    "type": "function",
                    "name": name,
                    "description": description,
                    "parameters": input_schema,
                    "strict": True,
                }
            )
        return translated

    async def _rpc(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        conversation_id: str,
    ) -> JsonObject:
        request_id = next(self._request_ids)
        request_body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": dict(params),
        }
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "x-conversation-id": conversation_id,
        }

        try:
            response = await self._http_client.post(
                self._endpoint, headers=headers, json=request_body
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise MCPClientError("MCP request timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise MCPClientError(
                f"MCP request failed with HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise MCPClientError("MCP request failed") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise MCPClientError("MCP response was not valid JSON") from exc

        if not isinstance(payload, dict):
            raise MCPClientError("MCP response was not a JSON object")
        if payload.get("id") != request_id:
            raise MCPClientError("MCP response ID did not match the request")
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code", "unknown")
            message = error.get("message", "JSON-RPC request failed")
            raise MCPClientError(f"MCP JSON-RPC error {code}: {message}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise MCPClientError("MCP response did not contain a result object")
        return result
