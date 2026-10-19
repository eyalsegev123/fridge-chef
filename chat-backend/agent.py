"""Azure OpenAI Responses orchestration for Fridge Chef."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Mapping, Optional, Sequence, Tuple

import httpx

from mcp_client import MCPClient, MCPClientError
from prompt import SYSTEM_PROMPT

logger = logging.getLogger(__name__)
JsonObject = Dict[str, Any]


@dataclass(frozen=True)
class AgentReply:
    """The model's text plus an optional recipe photo taken from tool data."""

    text: str
    photo_url: Optional[str] = None


class AzureOpenAIError(RuntimeError):
    """Raised when Azure OpenAI cannot produce a valid Responses payload."""


class AzureResponsesClient:
    """Small async adapter around the Azure OpenAI Responses endpoint."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        deployment: str,
        *,
        timeout: float = 60.0,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        if not endpoint.strip():
            raise ValueError("endpoint is required")
        if not api_key:
            raise ValueError("api_key is required")
        if not deployment.strip():
            raise ValueError("deployment is required")

        self._endpoint = endpoint
        self._api_key = api_key
        self._deployment = deployment
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        """Close the HTTP client when this instance created it."""
        if self._owns_http_client:
            await self._http_client.aclose()

    async def create_response(
        self,
        input_items: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> JsonObject:
        """Request one model response with the current conversation transcript."""
        request_body = {
            "model": self._deployment,
            "instructions": SYSTEM_PROMPT,
            "input": list(input_items),
            "tools": list(tools),
            "tool_choice": "auto",
        }
        try:
            response = await self._http_client.post(
                self._endpoint,
                headers={
                    "api-key": self._api_key,
                    "Content-Type": "application/json",
                },
                json=request_body,
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise AzureOpenAIError("Azure OpenAI request timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise AzureOpenAIError(
                f"Azure OpenAI returned HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise AzureOpenAIError("Azure OpenAI request failed") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise AzureOpenAIError("Azure OpenAI returned invalid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("output"), list):
            raise AzureOpenAIError("Azure OpenAI response did not contain output items")
        return payload


class ConversationHistory:
    """Keeps a bounded, concurrency-safe tool transcript for each conversation."""

    def __init__(self, max_turns: int = 10, max_conversations: int = 1_000) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be positive")
        if max_conversations < 1:
            raise ValueError("max_conversations must be positive")
        self._max_turns = max_turns
        self._max_conversations = max_conversations
        self._turns: OrderedDict[str, Deque[List[JsonObject]]] = OrderedDict()
        self._locks: Dict[str, asyncio.Lock] = {}
        self._reservations: Dict[str, int] = {}

    def reserve(self, conversation_id: str) -> asyncio.Lock:
        """Reserve and return a lock, counting both holders and queued waiters."""
        existing_lock = self._locks.get(conversation_id)
        if existing_lock is None:
            self._evict_oldest_inactive_conversation()
            existing_lock = asyncio.Lock()
            self._locks[conversation_id] = existing_lock
            self._turns[conversation_id] = deque(maxlen=self._max_turns)
        self._reservations[conversation_id] = (
            self._reservations.get(conversation_id, 0) + 1
        )
        self._touch(conversation_id)
        return existing_lock

    def release(self, conversation_id: str) -> None:
        """Release one holder/waiter reservation and enforce the idle cap."""
        reservations = self._reservations.get(conversation_id, 0)
        if reservations <= 1:
            self._reservations.pop(conversation_id, None)
        else:
            self._reservations[conversation_id] = reservations - 1
        self.prune_inactive_conversations()

    def input_items(self, conversation_id: str) -> List[JsonObject]:
        """Return an isolated flattened copy of the retained conversation turns."""
        self._touch(conversation_id)
        turns = self._turns.get(conversation_id, ())
        return [copy.deepcopy(item) for turn in turns for item in turn]

    def append_turn(self, conversation_id: str, items: List[JsonObject]) -> None:
        """Store one completed user/assistant turn and evict older turns."""
        turns = self._turns.setdefault(
            conversation_id, deque(maxlen=self._max_turns)
        )
        turns.append(copy.deepcopy(items))
        self._touch(conversation_id)

    def _touch(self, conversation_id: str) -> None:
        if conversation_id in self._turns:
            self._turns.move_to_end(conversation_id)

    def prune_inactive_conversations(self) -> None:
        """Enforce the conversation cap after active request locks are released."""
        while len(self._turns) > self._max_conversations:
            if not self._evict_oldest_inactive_conversation():
                return

    def _evict_oldest_inactive_conversation(self) -> bool:
        if len(self._turns) < self._max_conversations:
            return False
        for conversation_id in list(self._turns):
            if self._reservations.get(conversation_id, 0) > 0:
                continue
            self._turns.pop(conversation_id, None)
            self._locks.pop(conversation_id, None)
            self._reservations.pop(conversation_id, None)
            return True
        return False


class FridgeChefAgent:
    """Runs a bounded model/tool loop shared by HTTP chat and Telegram."""

    MAX_MODEL_ROUNDS = 4
    SERVICE_UNAVAILABLE_MESSAGE = (
        "I couldn't reach the recipe service right now. Please try again shortly."
    )
    LOOP_LIMIT_MESSAGE = (
        "I couldn't complete that recipe request safely. Please try a simpler request."
    )

    def __init__(
        self,
        azure_client: AzureResponsesClient,
        mcp_client: MCPClient,
        tools: Sequence[Mapping[str, Any]],
        *,
        history: Optional[ConversationHistory] = None,
    ) -> None:
        if not tools:
            raise ValueError("At least one discovered MCP tool is required")
        self._azure_client = azure_client
        self._mcp_client = mcp_client
        self._tools = [dict(tool) for tool in tools]
        self._history = history or ConversationHistory(max_turns=10)

    async def respond(self, conversation_id: str, message: str) -> AgentReply:
        """Respond to one message while preserving ordered per-chat context."""
        cleaned_message = message.strip()
        if not conversation_id:
            raise ValueError("conversation_id is required")
        if not cleaned_message:
            raise ValueError("message must not be empty")

        conversation_lock = self._history.reserve(conversation_id)
        try:
            async with conversation_lock:
                history_items = self._history.input_items(conversation_id)
                try:
                    reply, turn_items = await self._run_tool_loop(
                        conversation_id, cleaned_message, history_items
                    )
                except AzureOpenAIError as exc:
                    logger.warning("AZURE_RESPONSE_ERROR | %s", exc)
                    reply = AgentReply(self.SERVICE_UNAVAILABLE_MESSAGE)
                    turn_items = [
                        {"role": "user", "content": cleaned_message},
                        {"role": "assistant", "content": reply.text},
                    ]
                self._history.append_turn(conversation_id, turn_items)
        finally:
            self._history.release(conversation_id)
        return reply

    async def _run_tool_loop(
        self,
        conversation_id: str,
        message: str,
        history_items: List[JsonObject],
    ) -> Tuple[AgentReply, List[JsonObject]]:
        user_item: JsonObject = {"role": "user", "content": message}
        model_input = [*history_items, user_item]
        turn_items: List[JsonObject] = [user_item]
        # The photo comes from tool data, never from the model's text, so the
        # model cannot invent an image URL.
        photo_url: Optional[str] = None

        for round_number in range(1, self.MAX_MODEL_ROUNDS + 1):
            logger.info("AZURE_ROUND_START | round=%d", round_number)
            response = await self._azure_client.create_response(
                model_input, self._tools
            )
            output_items = response["output"]
            function_calls = self._function_calls(output_items)
            if not function_calls:
                reply = self._extract_text(response)
                if not reply:
                    raise AzureOpenAIError(
                        "Azure OpenAI response did not contain reply text"
                    )
                turn_items.append({"role": "assistant", "content": reply})
                return AgentReply(reply, photo_url), turn_items

            # The Responses API expects its output items back in the next input.
            model_input.extend(output_items)
            for function_call in function_calls:
                normalized_call = {
                    "type": "function_call",
                    "call_id": function_call["call_id"],
                    "name": function_call["name"],
                    "arguments": function_call["arguments"],
                }
                turn_items.append(normalized_call)
                envelope = await self._execute_tool(
                    conversation_id, function_call
                )
                photo_url = self._recipe_photo(envelope) or photo_url
                tool_output = {
                    "type": "function_call_output",
                    "call_id": function_call["call_id"],
                    "output": json.dumps(envelope, ensure_ascii=False),
                }
                model_input.append(tool_output)
                turn_items.append(tool_output)

        turn_items.append(
            {"role": "assistant", "content": self.LOOP_LIMIT_MESSAGE}
        )
        return AgentReply(self.LOOP_LIMIT_MESSAGE), turn_items

    async def _execute_tool(
        self, conversation_id: str, function_call: JsonObject
    ) -> JsonObject:
        name = function_call["name"]
        call_id = function_call["call_id"]
        started = time.perf_counter()
        try:
            arguments = json.loads(function_call["arguments"])
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError):
            envelope = self._system_error(
                "INVALID_TOOL_ARGUMENTS",
                "The model produced invalid tool arguments.",
                retryable=False,
            )
        else:
            try:
                envelope = await self._mcp_client.call_tool(
                    name, arguments, conversation_id=conversation_id
                )
            except MCPClientError:
                envelope = self._system_error(
                    "MCP_REQUEST_FAILED",
                    "The recipe service could not complete the tool request.",
                    retryable=True,
                )

        logger.info(
            "MCP_TOOL_DONE | name=%s call_id=%s status=%s elapsed=%.3fs",
            name,
            call_id,
            envelope.get("status", "unknown"),
            time.perf_counter() - started,
        )
        return envelope

    @staticmethod
    def _recipe_photo(envelope: Mapping[str, Any]) -> Optional[str]:
        """Return the thumbnail of a successfully fetched recipe, if any."""
        if envelope.get("status") != "success":
            return None
        data = envelope.get("data")
        # Only get_recipe_details returns a single recipe with a top-level
        # thumbnail; search results keep thumbnails inside their meal lists.
        if not isinstance(data, dict) or "instructions" not in data:
            return None
        thumbnail = data.get("thumbnail")
        if isinstance(thumbnail, str) and thumbnail.startswith("https://"):
            return thumbnail
        return None

    @staticmethod
    def _function_calls(output_items: Sequence[Any]) -> List[JsonObject]:
        calls: List[JsonObject] = []
        for item in output_items:
            if not isinstance(item, dict) or item.get("type") != "function_call":
                continue
            call_id = item.get("call_id")
            name = item.get("name")
            arguments = item.get("arguments")
            if not all(isinstance(value, str) and value for value in (call_id, name)):
                raise AzureOpenAIError("Azure OpenAI returned an invalid function call")
            if not isinstance(arguments, str):
                raise AzureOpenAIError("Azure OpenAI returned invalid function arguments")
            calls.append(
                {"call_id": call_id, "name": name, "arguments": arguments}
            )
        return calls

    @staticmethod
    def _extract_text(response: Mapping[str, Any]) -> str:
        direct_text = response.get("output_text")
        if isinstance(direct_text, str) and direct_text.strip():
            return direct_text.strip()

        text_parts: List[str] = []
        output_items = response.get("output", [])
        if not isinstance(output_items, list):
            return ""
        for item in output_items:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content_items = item.get("content")
            if not isinstance(content_items, list):
                continue
            for content in content_items:
                if not isinstance(content, dict):
                    continue
                text = content.get("text")
                if content.get("type") == "output_text" and isinstance(text, str):
                    stripped = text.strip()
                    if stripped:
                        text_parts.append(stripped)
        return "\n".join(text_parts)

    @staticmethod
    def _system_error(code: str, message: str, *, retryable: bool) -> JsonObject:
        return {
            "status": "system_error",
            "data": None,
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
            },
        }
