"""Network-free tests for the chat backend's HTTP routes and Telegram helpers.

Run with: python -m unittest -v test_backend.py
"""

import unittest
from typing import List, Tuple

from fastapi.testclient import TestClient

import main
from main import AppServices, TelegramTaskManager, _parse_text_message
from telegram import TelegramClient


class FakeAgent:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []

    async def respond(self, conversation_id: str, message: str) -> str:
        self.calls.append((conversation_id, message))
        return f"echo: {message}"


def build_client(chat_access_token: str = "") -> Tuple[TestClient, FakeAgent]:
    # Not used as a context manager, so the startup lifespan (which needs real
    # MCP, Azure and Telegram settings) does not run.
    app = main.create_app()
    agent = FakeAgent()
    app.state.services = AppServices(
        agent=agent,  # type: ignore[arg-type]
        telegram=TelegramClient("123:fake"),
        webhook_secret="s3cret",
        chat_access_token=chat_access_token,
    )
    return TestClient(app), agent


class ChatRouteTests(unittest.TestCase):
    def test_chat_is_open_when_no_token_is_configured(self) -> None:
        client, agent = build_client()
        response = client.post("/chat", json={"chat_id": "a", "message": "hi"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"reply": "echo: hi"})
        self.assertEqual(agent.calls, [("http:a", "hi")])

    def test_chat_requires_bearer_token_when_configured(self) -> None:
        client, agent = build_client(chat_access_token="letmein")
        denied = client.post("/chat", json={"chat_id": "a", "message": "hi"})
        allowed = client.post(
            "/chat",
            json={"chat_id": "a", "message": "hi"},
            headers={"Authorization": "Bearer letmein"},
        )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(len(agent.calls), 1)


class TelegramWebhookTests(unittest.TestCase):
    def test_wrong_secret_is_rejected(self) -> None:
        client, _agent = build_client()
        response = client.post(
            "/telegram/webhook",
            json={"message": {"chat": {"id": 1}, "text": "hi"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
        self.assertEqual(response.status_code, 403)

    def test_parse_text_message_ignores_non_text_updates(self) -> None:
        self.assertEqual(
            _parse_text_message({"message": {"chat": {"id": 7}, "text": " hi "}}),
            ("7", "hi"),
        )
        self.assertIsNone(_parse_text_message({"message": {"chat": {"id": 7}}}))
        self.assertIsNone(_parse_text_message({"edited_message": {}}))


class TelegramQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_chat_messages_are_processed_in_order(self) -> None:
        seen: List[str] = []

        async def processor(chat_id: str, text: str) -> None:
            seen.append(f"{chat_id}:{text}")

        manager = TelegramTaskManager(processor, max_pending=10, max_concurrent=2)
        for text in ("one", "two", "three"):
            self.assertTrue(manager.enqueue("c", text))
        await manager.close()
        self.assertEqual(seen, ["c:one", "c:two", "c:three"])


class SplitMessageTests(unittest.TestCase):
    def test_long_text_is_split_without_losing_content(self) -> None:
        text = ("word " * 2000).strip()
        chunks = TelegramClient.split_message(text)
        self.assertTrue(all(len(chunk) <= 4096 for chunk in chunks))
        self.assertEqual("".join(chunks), text)
        self.assertGreater(len(chunks), 1)


if __name__ == "__main__":
    unittest.main()
