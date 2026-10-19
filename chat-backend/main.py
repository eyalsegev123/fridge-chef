#!/usr/bin/env python3
"""FastAPI entry point for local chat and the Telegram webhook."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional, Set

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from agent import AgentReply, AzureResponsesClient, FridgeChefAgent
from mcp_client import MCPClient
from telegram import TelegramClient, TelegramClientError

logging.basicConfig(level=logging.INFO)
# Telegram requires the bot token in its URL, so HTTPX request URLs must not be
# emitted by the application logger.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent
TELEGRAM_BUSY_MESSAGE = (
    "I'm handling several recipe requests right now. Please try again shortly."
)
TELEGRAM_WELCOME_MESSAGE = (
    "Hi! I'm Fridge Chef. Tell me what's in your fridge, for example "
    "\"I have chicken and rice\", and I'll find real recipes you can make.\n\n"
    "You can also ask for a cuisine or a kind of dish, like "
    "\"something Italian\" or \"a vegetarian dish\"."
)


class ConfigurationError(RuntimeError):
    """Raised when required service configuration is missing or invalid."""


@dataclass(frozen=True, repr=False)
class Settings:
    """Validated runtime configuration without secret-bearing repr output."""

    mcp_server_url: str
    mcp_access_token: str
    azure_openai_api_key: str
    azure_openai_endpoint: str
    azure_openai_deployment: str
    telegram_bot_token: str
    telegram_webhook_secret: str
    chat_access_token: str = ""

    @classmethod
    def from_environment(cls) -> "Settings":
        """Load the local .env file and validate every required variable."""
        load_dotenv(BASE_DIR / ".env", override=False)

        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise ConfigurationError(f"{name} is required")
            return value

        settings = cls(
            mcp_server_url=required("MCP_SERVER_URL"),
            mcp_access_token=required("MCP_ACCESS_TOKEN"),
            azure_openai_api_key=required("AZURE_OPENAI_API_KEY"),
            azure_openai_endpoint=required("AZURE_OPENAI_ENDPOINT"),
            azure_openai_deployment=required("AZURE_OPENAI_DEPLOYMENT"),
            telegram_bot_token=required("TELEGRAM_BOT_TOKEN"),
            telegram_webhook_secret=required("TELEGRAM_WEBHOOK_SECRET"),
            # Optional. ngrok exposes every route on this port, including
            # /chat, so set this before tunnelling to keep /chat private.
            chat_access_token=os.getenv("CHAT_ACCESS_TOKEN", "").strip(),
        )
        if not settings.azure_openai_endpoint.startswith("https://"):
            raise ConfigurationError("AZURE_OPENAI_ENDPOINT must use HTTPS")
        if not re.fullmatch(
            r"[A-Za-z0-9_-]{1,256}", settings.telegram_webhook_secret
        ):
            raise ConfigurationError(
                "TELEGRAM_WEBHOOK_SECRET must contain only letters, digits, _ or -"
            )
        return settings


@dataclass(frozen=True)
class AppServices:
    """Runtime dependencies shared by the HTTP routes."""

    agent: FridgeChefAgent
    telegram: TelegramClient
    webhook_secret: str
    chat_access_token: str = ""


TelegramProcessor = Callable[[str, str], Awaitable[None]]


class TelegramTaskManager:
    """Runs bounded Telegram work while preserving FIFO order within each chat."""

    def __init__(
        self,
        processor: TelegramProcessor,
        *,
        max_pending: int = 100,
        max_concurrent: int = 4,
    ) -> None:
        if max_pending < 1 or max_concurrent < 1:
            raise ValueError("Telegram task limits must be positive")
        self._processor = processor
        self._max_pending = max_pending
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._pending: Set[asyncio.Task[None]] = set()
        self._chat_tails: Dict[str, asyncio.Task[None]] = {}

    def enqueue(self, chat_id: str, text: str) -> bool:
        """Schedule work without blocking the webhook response."""
        if len(self._pending) >= self._max_pending:
            return False
        previous_task = self._chat_tails.get(chat_id)
        task = asyncio.create_task(
            self._run_after(previous_task, chat_id, text),
            name=f"telegram-{chat_id}",
        )
        self._pending.add(task)
        self._chat_tails[chat_id] = task
        task.add_done_callback(
            lambda completed, current_chat_id=chat_id: self._task_finished(
                current_chat_id, completed
            )
        )
        return True

    async def close(self) -> None:
        """Wait for accepted webhook work during graceful shutdown."""
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    async def _run_after(
        self,
        previous_task: Optional[asyncio.Task[None]],
        chat_id: str,
        text: str,
    ) -> None:
        if previous_task is not None:
            await asyncio.gather(previous_task, return_exceptions=True)
        async with self._semaphore:
            await self._processor(chat_id, text)

    def _task_finished(
        self, chat_id: str, completed_task: asyncio.Task[None]
    ) -> None:
        self._pending.discard(completed_task)
        if self._chat_tails.get(chat_id) is completed_task:
            self._chat_tails.pop(chat_id, None)


class ChatRequest(BaseModel):
    chat_id: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=8_000)

    @field_validator("chat_id", "message")
    @classmethod
    def reject_blank_values(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value must not be blank")
        return cleaned


class ChatResponse(BaseModel):
    reply: str
    photo_url: Optional[str] = None


@asynccontextmanager
async def application_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build independent service clients and discover MCP tools at startup."""
    settings = Settings.from_environment()
    mcp_client = MCPClient(
        settings.mcp_server_url,
        settings.mcp_access_token,
    )
    azure_client = AzureResponsesClient(
        settings.azure_openai_endpoint,
        settings.azure_openai_api_key,
        settings.azure_openai_deployment,
    )
    telegram_client = TelegramClient(settings.telegram_bot_token)
    telegram_tasks: Optional[TelegramTaskManager] = None

    try:
        mcp_tools = await mcp_client.list_tools()
        azure_tools = MCPClient.to_azure_tools(mcp_tools)
        agent = FridgeChefAgent(azure_client, mcp_client, azure_tools)
        services = AppServices(
            agent=agent,
            telegram=telegram_client,
            webhook_secret=settings.telegram_webhook_secret,
            chat_access_token=settings.chat_access_token,
        )
        telegram_tasks = TelegramTaskManager(
            lambda chat_id, text: _process_telegram_message(
                services, chat_id, text
            )
        )
        app.state.services = services
        app.state.telegram_tasks = telegram_tasks
        logger.info("STARTUP_READY | discovered_tools=%d", len(azure_tools))
        yield
    finally:
        if telegram_tasks is not None:
            await telegram_tasks.close()
        await telegram_client.close()
        await azure_client.close()
        await mcp_client.close()


def create_app() -> FastAPI:
    """Create the FastAPI application without crossing the MCP code boundary."""
    application = FastAPI(
        title="Fridge Chef Chat Backend",
        description="Azure OpenAI agent and Telegram channel for Fridge Chef",
        version="1.0.0",
        lifespan=application_lifespan,
    )

    @application.get("/health")
    async def health() -> Dict[str, str]:
        return {"status": "healthy"}

    @application.post("/chat", response_model=ChatResponse)
    async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
        services = _get_services(request)
        if services.chat_access_token:
            supplied = request.headers.get("authorization", "")
            if not hmac.compare_digest(
                supplied, f"Bearer {services.chat_access_token}"
            ):
                raise HTTPException(status_code=401, detail="Unauthorized")
        reply = await services.agent.respond(
            f"http:{payload.chat_id}", payload.message
        )
        return ChatResponse(reply=reply.text, photo_url=reply.photo_url)

    @application.post("/telegram/webhook")
    async def telegram_webhook(request: Request) -> Dict[str, Any]:
        services = _get_services(request)
        supplied_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token", ""
        )
        if not supplied_secret or not hmac.compare_digest(
            supplied_secret, services.webhook_secret
        ):
            raise HTTPException(status_code=403, detail="Invalid webhook secret")

        try:
            update = await request.json()
        except ValueError:
            return {"ok": True}
        parsed_message = _parse_text_message(update)
        if parsed_message is None:
            return {"ok": True}

        chat_id, text = parsed_message
        task_manager = getattr(request.app.state, "telegram_tasks", None)
        if not isinstance(task_manager, TelegramTaskManager):
            raise HTTPException(status_code=503, detail="Service is not ready")
        if not task_manager.enqueue(chat_id, text):
            logger.warning("TELEGRAM_QUEUE_FULL | chat_id=%s", chat_id)
            # Telegram can execute one Bot API method directly from a webhook's
            # JSON response, preserving the required fast HTTP 200 acknowledgement.
            return {
                "method": "sendMessage",
                "chat_id": chat_id,
                "text": TELEGRAM_BUSY_MESSAGE,
            }
        return {"ok": True}

    return application


def _get_services(request: Request) -> AppServices:
    services = getattr(request.app.state, "services", None)
    if not isinstance(services, AppServices):
        raise HTTPException(status_code=503, detail="Service is not ready")
    return services


def _parse_text_message(update: Any) -> tuple[str, str] | None:
    if not isinstance(update, dict):
        return None
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    text = message.get("text")
    if not isinstance(chat, dict) or not isinstance(text, str):
        return None
    chat_id = chat.get("id")
    if not isinstance(chat_id, (str, int)) or isinstance(chat_id, bool):
        return None
    cleaned_text = text.strip()
    if not cleaned_text:
        return None
    return str(chat_id), cleaned_text


def _is_start_command(text: str) -> bool:
    # Telegram sends "/start" when someone opens the bot, and "/start@BotName"
    # in groups. Answering locally avoids spending a model call on it.
    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
    return command in {"/start", "/help"}


async def _process_telegram_message(
    services: AppServices, chat_id: str, text: str
) -> None:
    if _is_start_command(text):
        try:
            await services.telegram.send_message(chat_id, TELEGRAM_WELCOME_MESSAGE)
        except TelegramClientError:
            logger.error("TELEGRAM_REPLY_FAILED | chat_id=%s", chat_id)
        return

    try:
        await services.telegram.send_typing(chat_id)
    except TelegramClientError:
        logger.warning("TELEGRAM_TYPING_FAILED | chat_id=%s", chat_id)

    try:
        reply = await services.agent.respond(f"telegram:{chat_id}", text)
    except Exception:
        logger.exception("TELEGRAM_AGENT_FAILED | chat_id=%s", chat_id)
        reply = AgentReply(FridgeChefAgent.SERVICE_UNAVAILABLE_MESSAGE)

    if reply.photo_url:
        try:
            await services.telegram.send_photo(chat_id, reply.photo_url)
        except TelegramClientError:
            # A missing picture should never block the recipe text.
            logger.warning("TELEGRAM_PHOTO_FAILED | chat_id=%s", chat_id)

    try:
        await services.telegram.send_message(chat_id, reply.text)
    except TelegramClientError:
        logger.error("TELEGRAM_REPLY_FAILED | chat_id=%s", chat_id)


app = create_app()


if __name__ == "__main__":
    load_dotenv(BASE_DIR / ".env", override=False)
    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
