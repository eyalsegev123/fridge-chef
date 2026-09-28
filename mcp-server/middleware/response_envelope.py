"""Shared response envelope for every MCP tool."""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def wrap_success(data: Any) -> Dict[str, Any]:
    logger.info("FN wrap_success")
    return {"status": "success", "data": data, "error": None}


def wrap_user_fixable(
    code: str,
    message: str,
    data: Any = None,
    retryable: bool = True,
) -> Dict[str, Any]:
    logger.info("FN wrap_user_fixable | code=%s retryable=%s", code, retryable)
    return {
        "status": "user_fixable",
        "data": data,
        "error": {"code": code, "message": message, "retryable": retryable},
    }


def wrap_system_error(
    code: str,
    message: str,
    retryable: bool = True,
    data: Any = None,
) -> Dict[str, Any]:
    logger.info("FN wrap_system_error | code=%s retryable=%s", code, retryable)
    return {
        "status": "system_error",
        "data": data,
        "error": {"code": code, "message": message, "retryable": retryable},
    }


def classify_tool_response(result: Any) -> Optional[Dict[str, Any]]:
    """Recognize common technical error objects returned by backend adapters.

    Project-specific business outcomes belong in tool implementations. Add new
    technical codes here only when they represent infrastructure failures.
    """
    if not isinstance(result, dict):
        return None

    code = str(result.get("error") or result.get("code") or "")
    if code not in {
        "CONFIG_ERROR",
        "HTTP_ERROR",
        "REQUEST_FAILED",
        "TIMEOUT",
        "UNEXPECTED_ERROR",
    }:
        return None

    logger.info("FN classify_tool_response | technical error=%s", code)
    return {
        "code": code,
        "message": str(result.get("message") or "Technical request failed."),
        "retryable": code != "CONFIG_ERROR",
    }
