"""Network-free tests for the chat backend's HTTP routes and Telegram helpers.

Run with: python -m unittest -v test_backend.py
"""

import unittest
from typing import List, Optional, Tuple

from fastapi.testclient import TestClient

import main
from agent import AgentReply, FridgeChefAgent
from main import (
    TELEGRAM_WELCOME_MESSAGE,
    AppServices,
    TelegramTaskManager,
    _parse_text_message,
    _process_telegram_message,
)
from telegram import TelegramClient


class FakeAgent:
    def __init__(self, photo_url: Optional[str] = None) -> None:
        self.calls: List[Tuple[str, str]] = []
        self.photo_url = photo_url

    async def respond(self, conversation_id: str, message: str) -> AgentReply:
        self.calls.append((conversation_id, message))
        return AgentReply(f"echo: {message}", self.photo_url)


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: List[Tuple[str, str]] = []

    async def send_typing(self, chat_id: str) -> None:
        self.sent.append(("typing", chat_id))

    async def send_photo(self, chat_id: str, photo_url: str) -> None:
        self.sent.append(("photo", photo_url))

    async def send_message(self, chat_id: str, text: str) -> None:
        self.sent.append(("message", text))


def fake_services(agent: FakeAgent, telegram: FakeTelegram) -> AppServices:
    return AppServices(
        agent=agent,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
        webhook_secret="s3cret",
    )


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
        self.assertEqual(response.json(), {"reply": "echo: hi", "photo_url": None})
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


class TelegramProcessingTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_command_answers_without_the_model(self) -> None:
        agent, telegram = FakeAgent(), FakeTelegram()
        await _process_telegram_message(
            fake_services(agent, telegram), "1", "/start"
        )
        self.assertEqual(agent.calls, [])
        self.assertEqual(telegram.sent, [("message", TELEGRAM_WELCOME_MESSAGE)])

    async def test_recipe_photo_is_sent_before_the_text(self) -> None:
        agent = FakeAgent(photo_url="https://img.example/1.jpg")
        telegram = FakeTelegram()
        await _process_telegram_message(
            fake_services(agent, telegram), "1", "show me the first one"
        )
        self.assertEqual(
            telegram.sent,
            [
                ("typing", "1"),
                ("photo", "https://img.example/1.jpg"),
                ("message", "echo: show me the first one"),
            ],
        )


class RecipePhotoTests(unittest.TestCase):
    def test_photo_comes_only_from_successful_recipe_details(self) -> None:
        details = {
            "status": "success",
            "data": {"instructions": "Cook.", "thumbnail": "https://img/1.jpg"},
        }
        search = {
            "status": "success",
            "data": {"meals": [{"thumbnail": "https://img/2.jpg"}]},
        }
        failed = {"status": "user_fixable", "data": None}
        self.assertEqual(FridgeChefAgent._recipe_photo(details), "https://img/1.jpg")
        self.assertIsNone(FridgeChefAgent._recipe_photo(search))
        self.assertIsNone(FridgeChefAgent._recipe_photo(failed))


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
