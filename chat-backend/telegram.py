"""Telegram Bot API adapter and message-size handling."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx


class TelegramClientError(RuntimeError):
    """Raised when Telegram rejects or cannot receive a bot request."""


class TelegramClient:
    """Sends typing indicators and plain-text messages through Telegram."""

    MAX_MESSAGE_LENGTH = 4096

    def __init__(
        self,
        bot_token: str,
        *,
        timeout: float = 15.0,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        if not bot_token:
            raise ValueError("bot_token is required")
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        """Close the HTTP client when this instance created it."""
        if self._owns_http_client:
            await self._http_client.aclose()

    async def send_typing(self, chat_id: str) -> None:
        """Tell Telegram that the bot is preparing a reply."""
        await self._post(
            "sendChatAction", {"chat_id": chat_id, "action": "typing"}
        )

    async def send_message(self, chat_id: str, text: str) -> None:
        """Send a reply in ordered chunks that satisfy Telegram's size limit."""
        for chunk in self.split_message(text):
            await self._post("sendMessage", {"chat_id": chat_id, "text": chunk})

    async def _post(self, method: str, payload: Dict[str, Any]) -> None:
        try:
            response = await self._http_client.post(
                f"{self._base_url}/{method}", json=payload
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise TelegramClientError(f"Telegram {method} timed out") from exc
        except httpx.HTTPError as exc:
            raise TelegramClientError(f"Telegram {method} request failed") from exc

        try:
            response_payload = response.json()
        except ValueError as exc:
            raise TelegramClientError(
                f"Telegram {method} returned invalid JSON"
            ) from exc
        if not isinstance(response_payload, dict) or response_payload.get("ok") is not True:
            raise TelegramClientError(f"Telegram {method} rejected the request")

    @classmethod
    def split_message(cls, text: str, limit: int = MAX_MESSAGE_LENGTH) -> List[str]:
        """Split text without reordering content, preferring readable boundaries."""
        if limit < 1:
            raise ValueError("limit must be positive")
        if not text:
            return []

        chunks: List[str] = []
        remaining = text
        while len(remaining) > limit:
            window = remaining[:limit]
            split_at = cls._preferred_split(window, limit)
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]
        if remaining:
            chunks.append(remaining)
        return chunks

    @staticmethod
    def _preferred_split(window: str, limit: int) -> int:
        earliest_readable_break = max(1, limit // 2)
        for separator in ("\n\n", "\n", " "):
            index = window.rfind(separator, earliest_readable_break, limit + 1)
            if index >= earliest_readable_break:
                return index + len(separator)
        return limit
