# Fridge Chef MCP Server

This directory contains the independent MCP service for Fridge Chef. It exposes two authenticated JSON-RPC tools over HTTP and calls TheMealDB through a single timed backend-request helper.

For complete architecture, chat-backend setup, Telegram/ngrok instructions, and demo flows, see the repository root `README.md`.

## Boundaries

- `http_server.py` owns FastAPI transport, bearer authentication, request correlation, redacted logs, and process lifecycle.
- `tools_definitions.py` is the authoritative source for model-facing tool names, descriptions, and input schemas.
- `server_simple.py` owns JSON-RPC routing, tool dispatch, TheMealDB calls, matching, and recipe normalization.
- `middleware/response_envelope.py` owns the shared `success`, `user_fixable`, and `system_error` contract.
- `test_smoke.py` verifies tool behavior with mocked HTTP responses and never contacts TheMealDB.

The sibling chat backend does not import this service. It discovers tools and invokes them only through authenticated HTTP requests to `POST /mcp`.

## Tools

- `find_recipes_by_ingredients`: intersects results from concurrent ingredient filters and falls back to ranked partial matches when necessary.
- `get_recipe_details`: converts TheMealDB's numbered ingredient and measure fields into a compact recipe.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Set `MCP_ACCESS_TOKEN` in `.env`, then load it and start the service:

```bash
set -a
source .env
set +a
python http_server.py
```

The default address is `http://127.0.0.1:8080`.

## Test

```bash
source .venv/bin/activate
python -m unittest -v test_smoke.py
```

## Logging

- `mcp_server.log` is a rotating process log.
- `conv_logs/<conversation-id>.log` contains correlated tool calls, results, timing, and outbound HTTP traces.
- Credential-shaped fields are recursively redacted before logging.
- Runtime logs, `.env`, bytecode, and virtual environments are excluded by the repository root `.gitignore`.
