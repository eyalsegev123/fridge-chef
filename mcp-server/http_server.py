#!/usr/bin/env python3
"""FastAPI transport, authentication, health checks, and structured logging."""

import hmac
import json
import logging
import os
import re
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from server_simple import FridgeChefServer, http_trace, request_backend_api_key

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
CONV_LOGS_DIR = BASE_DIR / "conv_logs"
SENSITIVE_KEY_PARTS = (
    "authorization",
    "secret",
    "token",
    "password",
    "api_key",
    "apikey",
    "email",
    "phone",
)


def _setup_file_logging() -> None:
    log_path = Path(os.getenv("LOG_FILE_PATH", str(BASE_DIR / "mcp_server.log")))
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logging.getLogger().addHandler(handler)
        logger.info("File logging initialised -> %s", log_path)
    except Exception as exc:
        logger.warning("Could not set up file logging: %s", exc)


_setup_file_logging()


def _safe_conversation_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value.strip())[:160] or "-"


def _conversation_id(request: Request) -> str:
    for header in ("x-conversation-id", "x-object-id", "x-session-id"):
        value = request.headers.get(header, "").strip()
        if value:
            return _safe_conversation_id(value)
    return "-"


def _redact(value: Any, field_name: str = "") -> Any:
    normalized = field_name.lower().replace("-", "_")
    if any(part in normalized for part in SENSITIVE_KEY_PARTS):
        return "***"
    if isinstance(value, dict):
        return {key: _redact(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _json(value: Any) -> str:
    return json.dumps(_redact(value), ensure_ascii=False, indent=2, default=str)


def _log_conversation(conversation_id: str, message: str) -> None:
    if conversation_id == "-":
        return
    try:
        CONV_LOGS_DIR.mkdir(parents=True, exist_ok=True)
        path = CONV_LOGS_DIR / f"{conversation_id}.log"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {message}\n")
    except Exception as exc:
        logger.warning("Conversation log write failed: %s", exc)


def _response_text(response: Any) -> str:
    try:
        return response["result"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return json.dumps(response, ensure_ascii=False, default=str)


def _tool_call_block(tool_name: str, arguments: Dict[str, Any]) -> str:
    return f"▶ TOOL CALL   {tool_name}\nargs:\n{_json(arguments)}"


def _tool_response_block(tool_name: str, response: Any, elapsed: float) -> str:
    raw = _response_text(response)
    try:
        formatted = _json(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        formatted = raw
    return f"◀ RESPONSE    {tool_name}   ({elapsed:.3f}s)\n{formatted}\n{'─' * 60}"


def _trace_block(trace: Any) -> str:
    if not trace:
        return ""
    return "\n".join(
        f"  ⇢ {item.get('method')} {item.get('url')} -> "
        f"{item.get('status')} ({item.get('elapsed', 0):.3f}s)"
        for item in trace
    )


def _require_mcp_auth(request: Request) -> None:
    expected = os.getenv("MCP_ACCESS_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=500, detail="MCP_ACCESS_TOKEN is not configured")
    supplied = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not supplied.startswith(prefix) or not hmac.compare_digest(
        supplied[len(prefix) :], expected
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _get_backend_api_key(request: Request) -> str:
    """Read the per-request downstream credential without logging it."""
    return request.headers.get("x-backend-api-key", "").strip()


server: Optional[FridgeChefServer] = None


def get_server() -> FridgeChefServer:
    global server
    if server is None:
        server = FridgeChefServer(str(BASE_DIR / "config.json"))
    return server


app = FastAPI(
    title="MCP Template Via TIFF",
    description="Generic MCP server template",
    version="1.0.0",
)

cors_origins = [
    item.strip()
    for item in os.getenv("CORS_ORIGINS", "").split(",")
    if item.strip()
]
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )


@app.get("/")
async def root() -> Dict[str, Any]:
    current = get_server()
    return {
        "name": current.config.server_name,
        "version": current.config.version,
        "status": "running",
    }


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "healthy"}


@app.get("/info")
async def info() -> Dict[str, Any]:
    current = get_server()
    return {
        "name": current.config.server_name,
        "version": current.config.version,
        "protocol": "mcp",
        "protocolVersion": "2024-11-05",
        "capabilities": ["tools"],
        "tools": [tool["name"] for tool in current.tools],
    }


@app.get("/mcp")
async def mcp_info() -> Dict[str, Any]:
    return await info()


@app.options("/mcp")
async def mcp_options() -> Dict[str, Any]:
    return {}


async def _handle_one(
    request_body: Dict[str, Any], conversation_id: str, backend_api_key: str
) -> Optional[Dict[str, Any]]:
    started = time.perf_counter()
    method = request_body.get("method")
    tool_name = "?"
    if method == "tools/call":
        params = request_body.get("params") or {}
        tool_name = str(params.get("name") or "?")
        arguments = params.get("arguments") or {}
        logger.info(
            "[%s] TOOL_CALL | %s | args=%s",
            conversation_id,
            tool_name,
            json.dumps(_redact(arguments), ensure_ascii=False),
        )
        _log_conversation(
            conversation_id, _tool_call_block(tool_name, arguments)
        )
        http_trace.set([])
    else:
        logger.info("[%s] MCP_REQUEST | method=%s", conversation_id, method)

    credential_token = request_backend_api_key.set(backend_api_key or None)
    try:
        response = await get_server().handle_request(request_body)
    finally:
        request_backend_api_key.reset(credential_token)
    elapsed = time.perf_counter() - started

    if method == "tools/call":
        logger.info(
            "[%s] TOOL_DONE | %s | %.3fs", conversation_id, tool_name, elapsed
        )
        trace = _trace_block(http_trace.get())
        if trace:
            _log_conversation(conversation_id, trace)
        _log_conversation(
            conversation_id, _tool_response_block(tool_name, response, elapsed)
        )
    return response


@app.post("/mcp")
async def mcp_endpoint(request: Request) -> Any:
    _require_mcp_auth(request)
    conversation_id = _conversation_id(request)
    backend_api_key = _get_backend_api_key(request)
    started = time.perf_counter()
    body: Any = None
    try:
        body = await request.json()
        if isinstance(body, list):
            responses = []
            for item in body:
                if not isinstance(item, dict):
                    responses.append(
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32600, "message": "Invalid Request"},
                        }
                    )
                    continue
                response = await _handle_one(item, conversation_id, backend_api_key)
                if response is not None:
                    responses.append(response)
            logger.info(
                "[%s] BATCH_DONE | %d responses | %.3fs",
                conversation_id,
                len(responses),
                time.perf_counter() - started,
            )
            return responses
        if not isinstance(body, dict):
            raise ValueError("JSON-RPC body must be an object or an array")
        return (await _handle_one(body, conversation_id, backend_api_key)) or {}
    except Exception as exc:
        elapsed = time.perf_counter() - started
        logger.error(
            "[%s] ERROR | %.3fs | %s", conversation_id, elapsed, exc, exc_info=True
        )
        _log_conversation(
            conversation_id, f"✖ ERROR       ({elapsed:.3f}s)\n{exc}\n{'─' * 60}"
        )
        return {
            "jsonrpc": "2.0",
            "id": body.get("id") if isinstance(body, dict) else None,
            "error": {"code": -32603, "message": "Internal error"},
        }


@app.get("/logs")
async def list_logs(request: Request) -> Dict[str, Any]:
    _require_mcp_auth(request)
    if not CONV_LOGS_DIR.exists():
        return {"conversations": []}
    return {
        "conversations": sorted(path.stem for path in CONV_LOGS_DIR.glob("*.log"))
    }


@app.get("/logs/{conversation_id}", response_class=PlainTextResponse)
async def conversation_log(conversation_id: str, request: Request) -> PlainTextResponse:
    _require_mcp_auth(request)
    safe_id = _safe_conversation_id(conversation_id)
    path = CONV_LOGS_DIR / f"{safe_id}.log"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Conversation log not found")
    return PlainTextResponse(path.read_text(encoding="utf-8"))


@app.on_event("shutdown")
async def shutdown_event() -> None:
    logger.info("FN shutdown_event | server shutting down")
    if server is not None:
        await server.cleanup()


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
