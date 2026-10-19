# Fridge Chef

Fridge Chef is a Telegram-based cooking assistant that finds recipes using ingredients a person already has. It uses Azure OpenAI for conversation and tool selection, but every recipe fact comes from [TheMealDB](https://www.themealdb.com/api.php) through a separate MCP server.

The project is intentionally split into two independently running Python services:

- **MCP server:** owns tool schemas, recipe matching, TheMealDB integration, response envelopes, and MCP conversation logs.
- **Chat backend:** owns Azure OpenAI orchestration, short-term chat history, the local chat API, Telegram webhook validation, and Telegram delivery.

The chat backend never imports the MCP server's Python code. Their only integration is authenticated JSON-RPC over HTTP.

## Architecture

```text
 Telegram user
      |
      v
 Telegram Bot API
      |
      | HTTPS webhook
      v
+---------------------------------------------------------------+
| chat-backend                                                  |
|                                                               |
|  POST /telegram/webhook ----+                                 |
|                             |                                 |
|  POST /chat ----------------+--> FridgeChefAgent              |
|                                   |                           |
|                                   | Azure Responses API       |
|                                   v                           |
|                              Azure OpenAI                     |
|                                   |                           |
|                                   | function_call             |
|                                   v                           |
|                              MCPClient                        |
+-----------------------------------|---------------------------+
                                    |
                                    | JSON-RPC 2.0
                                    | POST /mcp
                                    | Authorization: Bearer ...
                                    | x-conversation-id: ...
                                    v
+---------------------------------------------------------------+
| mcp-server                                                    |
|                                                               |
|  HTTP transport -> tool dispatch -> response envelope         |
|                                   |                           |
|                                   | GET filter.php / lookup.php
+-----------------------------------|---------------------------+
                                    |
                                    v
                               TheMealDB

Return path:
TheMealDB -> MCP envelope -> function_call_output -> Azure grounded answer
          -> chat-backend -> Telegram sendMessage -> Telegram user
```

### One Request, End to End

1. Telegram sends an update to `POST /telegram/webhook` on the chat backend.
2. The backend compares `X-Telegram-Bot-Api-Secret-Token` with `TELEGRAM_WEBHOOK_SECRET` using a constant-time comparison.
3. A valid text update receives HTTP 200 immediately. Processing enters a bounded FIFO background queue so Telegram does not retry while Azure and TheMealDB are working. If the queue is saturated, the HTTP 200 response asks Telegram to send a short busy message directly.
4. The backend sends Telegram's `typing` chat action and passes the message plus up to ten prior conversation turns to Azure OpenAI.
5. Azure selects a function from schemas discovered from the MCP server during backend startup. The schemas are not hardcoded in the chat service.
6. The chat backend sends an authenticated JSON-RPC `tools/call` request to the MCP server and includes the Telegram chat ID in `x-conversation-id`.
7. The MCP server calls TheMealDB through its single timed HTTP helper, normalizes the result, and returns the shared `success`, `user_fixable`, or `system_error` envelope.
8. The envelope becomes a Responses API `function_call_output`. Azure produces a user-facing answer using only tool data.
9. The chat backend splits replies at Telegram's 4096-character limit and sends each chunk in order.

The local `POST /chat` route enters the same agent at step 4. It is useful for development and for demonstrating the model/MCP flow without Telegram.

## Repository Layout

```text
fridge-chef/
├── README.md
├── .gitignore
├── mcp-server/
│   ├── http_server.py
│   ├── server_simple.py
│   ├── tools_definitions.py
│   ├── middleware/
│   │   ├── __init__.py
│   │   └── response_envelope.py
│   ├── config.json
│   ├── requirements.txt
│   ├── Dockerfile
│   ├── RESPONSE_CONTRACT.md
│   ├── test_smoke.py
│   └── .env.example
└── chat-backend/
    ├── main.py
    ├── agent.py
    ├── mcp_client.py
    ├── telegram.py
    ├── prompt.py
    ├── requirements.txt
    └── .env.example
```

### MCP Server Responsibilities

| File | Responsibility |
| --- | --- |
| `mcp-server/http_server.py` | FastAPI transport, bearer authentication, request correlation, redacted logging, and `/mcp`. |
| `mcp-server/tools_definitions.py` | The authoritative model-facing names, descriptions, and JSON schemas for both tools. |
| `mcp-server/server_simple.py` | JSON-RPC routing, tool dispatch, TheMealDB calls, matching logic, and compact recipe normalization. |
| `mcp-server/middleware/response_envelope.py` | Stable shared contract for success, user-correctable outcomes, and infrastructure failures. |
| `mcp-server/test_smoke.py` | Network-isolated contract tests using mocked TheMealDB responses. |

### Chat Backend Responsibilities

| File | Responsibility |
| --- | --- |
| `chat-backend/main.py` | Application lifecycle, configuration, `/chat`, secure Telegram webhook, and background delivery. |
| `chat-backend/agent.py` | Azure Responses client, four-round tool loop, and bounded per-chat history. |
| `chat-backend/mcp_client.py` | Authenticated MCP JSON-RPC client and dynamic schema translation. |
| `chat-backend/telegram.py` | Telegram typing, `sendMessage`, and readable 4096-character splitting. |
| `chat-backend/prompt.py` | Grounding, tool-order, partial-match, and error-relay policy. |

## MCP Tools

### `find_recipes_by_ingredients`

Use this first for every recipe request. It accepts one to five simple lowercase ingredients and sends one concurrent TheMealDB `filter.php` request per ingredient.

- If recipes contain every ingredient, it returns up to five exact matches.
- If the full intersection is empty, it ranks partial matches by the number of requested ingredients they contain.
- Each partial result states both `matched_ingredients` and `unmatched_ingredients`.
- If TheMealDB recognizes none of the terms, it returns `INGREDIENTS_NOT_RECOGNIZED` as a `user_fixable` result with an actionable suggestion.

### `browse_recipes_by_cuisine_or_category`

Use this when the user asks for a cuisine (`Italian`, `Indian`) or a kind of dish (`Dessert`, `Vegetarian`) instead of listing ingredients. It takes `cuisine`, `category`, or both (the other is `null`), calls TheMealDB `filter.php?a=` / `?c=` concurrently, and returns up to eight meals that match every filter, plus `total_matches`.

- Input is normalized to TheMealDB's title case, so `italian` works.
- An unknown value returns `UNKNOWN_CUISINE` or `UNKNOWN_CATEGORY` as `user_fixable`, with the valid options fetched live from `list.php`, so the model can offer real choices.

### `get_recipe_details`

Use this only after the user selects a meal returned by a search tool. It calls TheMealDB `lookup.php` and returns only:

- Name, category, cuisine area, and thumbnail.
- A clean list of ingredient/measure pairs collapsed from TheMealDB's 20 numbered fields.
- Instructions and source URL.

An unknown ID returns `MEAL_NOT_FOUND` as a `user_fixable` result.

When this tool succeeds in Telegram, the chat backend sends the recipe's thumbnail with `sendPhoto` before the text. The image URL is taken from the tool result, never from the model's reply, so the model cannot invent a picture.

### Telegram `/start`

`/start` and `/help` get an instant welcome message straight from the chat backend, without a model call.

## Response Contract

Every MCP tool result uses one outer envelope:

```json
{
  "status": "success | user_fixable | system_error",
  "data": {},
  "error": null
}
```

For a failure, `error` contains a stable machine-readable code, a diagnostic message, and whether retrying is reasonable. Business failures still arrive inside a successful JSON-RPC response, so the chat backend reads `result.structuredContent.status` rather than relying only on HTTP status.

## Prerequisites

- Python 3.11 or newer.
- An Azure OpenAI deployment that supports the Responses API.
- A Telegram bot created through [@BotFather](https://t.me/BotFather).
- [ngrok](https://ngrok.com/docs/getting-started/) for exposing the local Telegram webhook.
- No TheMealDB key or signup is required.

## Setup

### 1. Configure the MCP Server

```bash
cd mcp-server
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Set a long random `MCP_ACCESS_TOKEN` in `mcp-server/.env`. TheMealDB's public base URL is already configured in `mcp-server/config.json`.

Generate a suitable local token if needed:

```bash
openssl rand -hex 32
```

### 2. Configure the Chat Backend

```bash
cd ../chat-backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Fill in `chat-backend/.env`:

| Variable | Purpose |
| --- | --- |
| `MCP_SERVER_URL` | MCP service origin, normally `http://127.0.0.1:8080`. The client appends `/mcp`. |
| `MCP_ACCESS_TOKEN` | Exactly the same bearer token used by the MCP server. |
| `AZURE_OPENAI_API_KEY` | Server-side Azure credential. It is never returned to Telegram or `/chat`. |
| `AZURE_OPENAI_ENDPOINT` | Responses endpoint, including `api-version=preview`. |
| `AZURE_OPENAI_DEPLOYMENT` | Azure deployment name sent as the Responses API `model`. |
| `TELEGRAM_BOT_TOKEN` | BotFather token used only for Telegram Bot API calls. |
| `TELEGRAM_WEBHOOK_SECRET` | Random letters, digits, `_`, or `-`, used to authenticate incoming webhook requests. |
| `CHAT_ACCESS_TOKEN` | Optional. When set, `POST /chat` requires `Authorization: Bearer <token>`. Set it before running ngrok, which makes every route on port 8000 public, not only the webhook. |

All `.env` files are ignored by the root `.gitignore`. Do not paste secrets into source files, shell history, screenshots, chat messages, or the README.

## Quick Start: One Command

After the setup above, and with ngrok installed and authenticated:

```bash
./start.sh
```

It starts the MCP server, waits for it to be healthy, starts the chat backend (which discovers the tools), opens an ngrok tunnel, reads the new public URL from ngrok's local API, and registers it as the Telegram webhook with your secret. Both services' logs stream into the same window, with copies in `logs/`. Ctrl+C stops everything. Because the webhook is re-registered on every run, ngrok's changing URL is no longer a manual step.

Use `./start.sh --local` to run only the two services and test with `/chat`.

## Run Locally (Step by Step)

Run each service in its own terminal. Start the MCP server first because the chat backend discovers tool definitions during startup and intentionally fails fast if MCP is unavailable.

### Terminal 1: MCP Server on Port 8080

```bash
cd mcp-server
source .venv/bin/activate
set -a
source .env
set +a
python http_server.py
```

Check health:

```bash
curl http://127.0.0.1:8080/health
```

List MCP tools:

```bash
curl -X POST http://127.0.0.1:8080/mcp \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${MCP_ACCESS_TOKEN}" \
  -H 'x-conversation-id: manual-check' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

### Terminal 2: Chat Backend on Port 8000

```bash
cd chat-backend
source .venv/bin/activate
python main.py
```

The backend loads `chat-backend/.env` itself. On startup it calls MCP `tools/list`, validates the result, and translates the returned schemas for Azure OpenAI.

Check health:

```bash
curl http://127.0.0.1:8000/health
```

## Test the Local Chat Endpoint

Search using two ingredients:

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"chat_id":"demo-1","message":"I have chicken and rice. What can I make?"}'
```

Then keep the same `chat_id` for a follow-up:

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"chat_id":"demo-1","message":"Show me the first one in detail."}'
```

The same ID matters: history includes the prior tool result, allowing the model to resolve “the first one” to its real `meal_id` without displaying or inventing an ID.

## Connect Telegram Through ngrok

### 1. Expose the Chat Backend

With the chat backend running on port 8000:

```bash
ngrok http 8000
```

Copy the HTTPS forwarding origin, without a trailing slash:

```bash
export NGROK_URL='https://example.ngrok-free.app'
```

### 2. Register the Authenticated Webhook

Load the bot token and webhook secret in the shell without printing them, then register:

```bash
cd chat-backend
set -a
source .env
set +a

curl -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=${NGROK_URL}/telegram/webhook" \
  --data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}"
```

Telegram will include the secret in the `X-Telegram-Bot-Api-Secret-Token` header. Requests with a missing or incorrect value receive HTTP 403 and never enter the agent loop.

**Whenever the ngrok URL changes, run `setWebhook` again with the new URL.** Telegram continues sending updates to the previously registered address until it is replaced.

Verify the active registration:

```bash
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"
```

## Demo Scenarios

### Successful Flow

1. Send: `I have chicken and rice. What can I make?`
2. Explain that Azure chose `find_recipes_by_ingredients` from schemas discovered over MCP.
3. Show the two concurrent TheMealDB filter calls in the MCP log.
4. Confirm the bot presents a short list of real meal names.
5. Reply: `Show me the first one.`
6. Show Azure calling `get_recipe_details` with the retained meal ID and the bot returning exact measures and instructions.

### Browse by Cuisine, With a Photo

1. Send: `Something Indian please`.
2. Azure chooses `browse_recipes_by_cuisine_or_category` with `cuisine: "indian"`. The MCP log shows `filter.php?a=Indian`.
3. Pick one. `get_recipe_details` runs and the bot sends the dish photo, then the recipe.

### Graceful Failure

Send: `I have zzzznotfood.`

TheMealDB returns HTTP 200 with `{"meals": null}`. The MCP server converts that unusual API behavior into `status: user_fixable` with `INGREDIENTS_NOT_RECOGNIZED`. The prompt requires the bot to suggest a simpler term such as `chicken` instead of inventing a recipe or treating the null value as a crash.

## Tests

The MCP tests mock every outbound HTTP response; they never contact TheMealDB:

```bash
cd mcp-server
source .venv/bin/activate
python -m unittest -v test_smoke.py
```

The suite covers exact multi-ingredient matching, ranked partial matches, unrecognized terms, unknown meal IDs, ingredient/measure normalization, tool discovery, and equality between text and structured MCP results.

The chat backend tests replace the agent with a fake, so they need no MCP server, Azure key, or Telegram token:

```bash
cd chat-backend
source .venv/bin/activate
python -m unittest -v test_backend.py
```

They cover `/chat` authentication, webhook secret rejection, Telegram update parsing, same-chat FIFO ordering, and message splitting.

Useful syntax checks:

```bash
mcp-server/.venv/bin/python -m py_compile \
  mcp-server/server_simple.py \
  mcp-server/tools_definitions.py \
  mcp-server/http_server.py

chat-backend/.venv/bin/python -m py_compile \
  chat-backend/main.py \
  chat-backend/agent.py \
  chat-backend/mcp_client.py \
  chat-backend/telegram.py \
  chat-backend/prompt.py
```

## Logging and Security

- MCP access is protected by `Authorization: Bearer <MCP_ACCESS_TOKEN>`.
- Telegram webhook access is independently protected by its secret-token header.
- `/chat` is open by default for local development. Set `CHAT_ACCESS_TOKEN` whenever the backend is reachable through ngrok.
- Azure and Telegram credentials exist only in the chat backend process.
- The MCP transport recursively redacts fields whose names contain token-, secret-, password-, authorization-, API-key-, email-, or phone-related terms.
- MCP tool calls include `x-conversation-id`, producing correlated files under `mcp-server/conv_logs/` for local debugging. Those logs are ignored by Git.
- The chat backend logs tool names, call IDs, statuses, rounds, and durations, but not credentials or complete user messages.
- Do not send secrets as ingredients or chat text. Content-level secret detection is intentionally outside this project's scope.

## Operational Limits

- Conversation history is in memory and limited to ten completed turns per chat and 1,000 recent conversations. Restarting the chat backend clears it.
- Telegram processing allows at most 100 pending updates and four concurrent operations while preserving same-chat FIFO order. Saturated requests receive an immediate busy message through Telegram's webhook-response mechanism.
- Valid Telegram updates are acknowledged before background work completes. This avoids retries, but an unexpected process restart can lose accepted work because there is no durable queue.
- The Azure tool loop stops after four model rounds to prevent runaway calls.
- The chat backend fails startup when MCP schema discovery fails rather than silently using duplicated or stale schemas.
- TheMealDB is a public third-party API. Timeouts and upstream failures become safe tool errors rather than fabricated cooking advice.

## Project Guidance

This implementation follows the [MCP Server Challenge: Research, Build & Demo](https://docs.google.com/document/d/17VovvGbpsscBuAEleTVMrTp8qAMt187QMle8uSpqwA4/edit?tab=t.0) brief: a focused external API integration, model-oriented tool descriptions, explicit prompt behavior, a real Telegram chat path, and a demo that explains both one complete successful request and one handled edge case.
