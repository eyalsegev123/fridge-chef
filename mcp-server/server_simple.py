#!/usr/bin/env python3
"""Fridge Chef MCP server logic.

This module owns JSON-RPC routing, tool dispatch, response envelopes, and a
single reusable backend HTTP chokepoint. Transport and conversation logging
live in ``http_server.py``.
"""

import asyncio
import contextvars
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel, Field

from middleware.response_envelope import (
    classify_tool_response,
    wrap_success,
    wrap_system_error,
    wrap_user_fixable,
)
from tools_definitions import get_available_tools

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SUPPORTED_MCP_PROTOCOL_VERSIONS = ("2026-07-28", "2024-11-05")
MCP_CAPABILITIES: Dict[str, Any] = {"tools": {}}
MCP_DISCOVERY_TTL_MS = 300_000

# The HTTP transport sets a fresh trace per tool call. Backend adapters append
# redacted timing entries so they also appear in the conversation log.
http_trace: contextvars.ContextVar[Optional[List[Dict[str, Any]]]] = (
    contextvars.ContextVar("http_trace", default=None)
)

# The HTTP transport sets this value from the trusted x-backend-api-key
# request header and resets it after the request. Keeping the credential in a
# ContextVar prevents concurrent requests from sharing or persisting it.
request_backend_api_key: contextvars.ContextVar[Optional[str]] = (
    contextvars.ContextVar("request_backend_api_key", default=None)
)


class ServerConfig(BaseModel):
    server_name: str = "fridge-chef-mcp"
    version: str = "1.0.0"
    backend_base_url: str = ""
    timeout: float = Field(default=30.0, gt=0)


class FridgeChefServer:
    """MCP server core for finding and retrieving TheMealDB recipes."""

    def __init__(self, config_path: str = "config.json") -> None:
        self.config = self._load_config(config_path)
        self.tools = get_available_tools()
        self.http_client: Optional[httpx.AsyncClient] = None
        logger.info(
            "FN __init__ | FridgeChefServer ready | name=%s tools=%d",
            self.config.server_name,
            len(self.tools),
        )

    @staticmethod
    def _load_config(config_path: str) -> ServerConfig:
        path = Path(config_path)
        values: Dict[str, Any] = {}
        if path.exists():
            with path.open(encoding="utf-8") as handle:
                values = json.load(handle)
            logger.info("FN _load_config | loaded=%s", path)

        # Environment variables take precedence without storing secrets in Git.
        env_mapping = {
            "SERVER_NAME": "server_name",
            "SERVER_VERSION": "version",
            "BACKEND_BASE_URL": "backend_base_url",
            "BACKEND_TIMEOUT": "timeout",
        }
        for env_name, field_name in env_mapping.items():
            value = os.getenv(env_name)
            if value not in (None, ""):
                values[field_name] = value
        return ServerConfig(**values)

    async def _get_http_client(self) -> httpx.AsyncClient:
        if self.http_client is None:
            self.http_client = httpx.AsyncClient(timeout=self.config.timeout)
        return self.http_client

    async def cleanup(self) -> None:
        logger.info("FN cleanup")
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def _make_backend_request(
        self,
        endpoint: str,
        *,
        method: str = "POST",
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Reusable outbound HTTP helper for future tool implementations."""
        if not self.config.backend_base_url:
            return {
                "error": "CONFIG_ERROR",
                "message": "BACKEND_BASE_URL is not configured.",
            }

        url = (
            f"{self.config.backend_base_url.rstrip('/')}/"
            f"{endpoint.lstrip('/')}"
        )
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        backend_api_key = request_backend_api_key.get()
        if backend_api_key:
            headers["Authorization"] = f"Bearer {backend_api_key}"

        started = time.perf_counter()
        status: Any = "ERROR"
        try:
            client = await self._get_http_client()
            response = await client.request(
                method.upper(),
                url,
                json=json_data,
                params=params,
                headers=headers,
            )
            status = response.status_code
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Backend response must be a JSON object.")
            return payload
        except httpx.TimeoutException as exc:
            return {"error": "TIMEOUT", "message": str(exc)}
        except (httpx.HTTPError, ValueError) as exc:
            return {"error": "HTTP_ERROR", "message": str(exc)}
        finally:
            elapsed = time.perf_counter() - started
            logger.info(
                "HTTP %s %s -> %s | %.3fs", method.upper(), url, status, elapsed
            )
            trace = http_trace.get()
            if trace is not None:
                trace.append(
                    {
                        "method": method.upper(),
                        "url": url,
                        "status": status,
                        "elapsed": elapsed,
                    }
                )

    async def find_recipes_by_ingredients(
        self, ingredients: List[str]
    ) -> Dict[str, Any]:
        """Find full or ranked partial recipe matches for simple ingredients."""
        logger.info(
            "FN find_recipes_by_ingredients | ingredient_count=%d",
            len(ingredients) if isinstance(ingredients, list) else 0,
        )
        if not isinstance(ingredients, list) or not 1 <= len(ingredients) <= 5:
            return wrap_user_fixable(
                "INVALID_INGREDIENTS",
                "Provide between one and five simple lowercase ingredients.",
                data={"ingredients": ingredients},
                retryable=True,
            )

        cleaned: List[str] = []
        invalid: List[Any] = []
        for ingredient in ingredients:
            if not isinstance(ingredient, str):
                invalid.append(ingredient)
                continue
            # Forgive casing and extra spaces ("Chicken  Breast"); still reject
            # quantities, punctuation, and comma-separated lists.
            value = " ".join(ingredient.split()).lower()
            if not re.fullmatch(
                r"[a-z]+(?: [a-z]+)*", value
            ):
                invalid.append(ingredient)
                continue
            cleaned.append(value)

        if invalid:
            return wrap_user_fixable(
                "INVALID_INGREDIENTS",
                (
                    "Each ingredient must be a simple lowercase name without "
                    "quantities, punctuation, or comma-separated values."
                ),
                data={"invalid_ingredients": invalid},
                retryable=True,
            )

        responses = await asyncio.gather(
            *(
                self._make_backend_request(
                    "filter.php", method="GET", params={"i": ingredient}
                )
                for ingredient in cleaned
            )
        )

        meal_ids_by_ingredient: List[set[str]] = []
        meals_by_id: Dict[str, Dict[str, str]] = {}
        for ingredient, response in zip(cleaned, responses):
            if classify_tool_response(response):
                return response
            if "meals" not in response:
                return {
                    "error": "HTTP_ERROR",
                    "message": "TheMealDB response did not contain a meals field.",
                }

            backend_meals = response["meals"]
            if backend_meals is None:
                meal_ids_by_ingredient.append(set())
                continue
            if not isinstance(backend_meals, list):
                return {
                    "error": "HTTP_ERROR",
                    "message": "TheMealDB meals field was not a list or null.",
                }

            ingredient_meal_ids: set[str] = set()
            for meal in backend_meals:
                if not isinstance(meal, dict):
                    continue
                meal_id = str(meal.get("idMeal") or "").strip()
                if not meal_id:
                    continue
                ingredient_meal_ids.add(meal_id)
                meals_by_id.setdefault(
                    meal_id,
                    {
                        "meal_id": meal_id,
                        "name": str(meal.get("strMeal") or "").strip(),
                        "thumbnail": str(meal.get("strMealThumb") or "").strip(),
                    },
                )
            meal_ids_by_ingredient.append(ingredient_meal_ids)

        if not meals_by_id:
            return wrap_user_fixable(
                "INGREDIENTS_NOT_RECOGNIZED",
                (
                    "No recipes matched any supplied ingredient. Try simpler "
                    "ingredient names, such as 'chicken' instead of 'chicken thighs'."
                ),
                data={
                    "unrecognized_ingredients": cleaned,
                    "suggested_action": "Use simpler, singular ingredient names.",
                },
                retryable=True,
            )

        common_meal_ids = set.intersection(*meal_ids_by_ingredient)

        def format_meal(meal_id: str, partial: bool) -> Dict[str, Any]:
            matched = [
                ingredient
                for ingredient, meal_ids in zip(cleaned, meal_ids_by_ingredient)
                if meal_id in meal_ids
            ]
            result: Dict[str, Any] = {
                **meals_by_id[meal_id],
                "matched_count": len(matched),
                "matched_ingredients": matched,
            }
            if partial:
                result["unmatched_ingredients"] = [
                    ingredient for ingredient in cleaned if ingredient not in matched
                ]
            return result

        if common_meal_ids:
            ranked_ids = sorted(
                common_meal_ids,
                key=lambda meal_id: (
                    meals_by_id[meal_id]["name"].lower(),
                    meal_id,
                ),
            )[:5]
            return {
                "partial_match": False,
                "meals": [format_meal(meal_id, False) for meal_id in ranked_ids],
            }

        ranked_ids = sorted(
            meals_by_id,
            key=lambda meal_id: (
                -sum(
                    meal_id in meal_ids for meal_ids in meal_ids_by_ingredient
                ),
                meals_by_id[meal_id]["name"].lower(),
                meal_id,
            ),
        )[:5]
        return {
            "partial_match": True,
            "meals": [format_meal(meal_id, True) for meal_id in ranked_ids],
        }

    async def browse_recipes_by_cuisine_or_category(
        self, cuisine: Optional[str] = None, category: Optional[str] = None
    ) -> Dict[str, Any]:
        """List meals for a cuisine (TheMealDB "area"), a category, or both."""
        logger.info(
            "FN browse_recipes_by_cuisine_or_category | cuisine=%s category=%s",
            cuisine,
            category,
        )
        filters: Dict[str, str] = {}
        for param, value in (("a", cuisine), ("c", category)):
            if value is None:
                continue
            if not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z]+(?: [A-Za-z]+)*", value.strip()
            ):
                return wrap_user_fixable(
                    "INVALID_FILTER",
                    "cuisine and category must be plain words such as 'Italian'.",
                    data={"cuisine": cuisine, "category": category},
                    retryable=True,
                )
            # TheMealDB stores names in title case ("Italian", "Seafood").
            filters[param] = " ".join(value.split()).title()
        if not filters:
            return wrap_user_fixable(
                "MISSING_FILTER",
                "Provide a cuisine, a category, or both.",
                retryable=True,
            )

        responses = await asyncio.gather(
            *(
                self._make_backend_request(
                    "filter.php", method="GET", params={param: value}
                )
                for param, value in filters.items()
            )
        )
        meal_sets: List[set[str]] = []
        meals_by_id: Dict[str, Dict[str, str]] = {}
        for (param, value), response in zip(filters.items(), responses):
            if classify_tool_response(response):
                return response
            backend_meals = response.get("meals")
            if not backend_meals:
                # Unknown names come back as {"meals": null}; answer with the
                # valid choices so the model can correct itself.
                return await self._unrecognized_filter(param, value)
            ids: set[str] = set()
            for meal in backend_meals:
                if not isinstance(meal, dict) or not meal.get("idMeal"):
                    continue
                meal_id = str(meal["idMeal"])
                ids.add(meal_id)
                meals_by_id.setdefault(
                    meal_id,
                    {
                        "meal_id": meal_id,
                        "name": str(meal.get("strMeal") or "").strip(),
                        "thumbnail": str(meal.get("strMealThumb") or "").strip(),
                    },
                )
            meal_sets.append(ids)

        matching_ids = sorted(
            set.intersection(*meal_sets),
            key=lambda meal_id: (meals_by_id[meal_id]["name"].lower(), meal_id),
        )
        return {
            "cuisine": filters.get("a"),
            "category": filters.get("c"),
            "total_matches": len(matching_ids),
            "meals": [meals_by_id[meal_id] for meal_id in matching_ids[:8]],
        }

    async def _unrecognized_filter(self, param: str, value: str) -> Dict[str, Any]:
        field_name, list_key = {
            "a": ("cuisine", "strArea"),
            "c": ("category", "strCategory"),
        }[param]
        listing = await self._make_backend_request(
            "list.php", method="GET", params={param: "list"}
        )
        options: List[str] = []
        if not classify_tool_response(listing) and isinstance(
            listing.get("meals"), list
        ):
            options = sorted(
                str(item[list_key])
                for item in listing["meals"]
                if isinstance(item, dict) and item.get(list_key)
            )
        return wrap_user_fixable(
            f"UNKNOWN_{field_name.upper()}",
            f"'{value}' is not a {field_name} TheMealDB knows, or it has no recipes.",
            data={field_name: value, f"valid_{field_name}_options": options},
            retryable=True,
        )

    async def get_recipe_details(self, meal_id: str) -> Dict[str, Any]:
        """Return a compact recipe with normalized ingredient/measure pairs."""
        logger.info("FN get_recipe_details | meal_id=%s", meal_id)
        if not isinstance(meal_id, str) or not re.fullmatch(r"[0-9]+", meal_id):
            return wrap_user_fixable(
                "INVALID_MEAL_ID",
                "meal_id must be a digits-only string from a recipe search result.",
                data={"meal_id": meal_id},
                retryable=True,
            )

        response = await self._make_backend_request(
            "lookup.php", method="GET", params={"i": meal_id}
        )
        if classify_tool_response(response):
            return response
        if "meals" not in response:
            return {
                "error": "HTTP_ERROR",
                "message": "TheMealDB response did not contain a meals field.",
            }

        backend_meals = response["meals"]
        if backend_meals is None or backend_meals == []:
            return wrap_user_fixable(
                "MEAL_NOT_FOUND",
                "No recipe exists for that meal_id. Choose a meal from the search results.",
                data={"meal_id": meal_id},
                retryable=True,
            )
        if not isinstance(backend_meals, list) or not isinstance(
            backend_meals[0], dict
        ):
            return {
                "error": "HTTP_ERROR",
                "message": "TheMealDB meals field was not a recipe list or null.",
            }

        meal = backend_meals[0]
        ingredient_pairs: List[Dict[str, str]] = []
        for index in range(1, 21):
            ingredient_value = meal.get(f"strIngredient{index}")
            ingredient = (
                ingredient_value.strip()
                if isinstance(ingredient_value, str)
                else ""
            )
            if not ingredient:
                continue
            measure_value = meal.get(f"strMeasure{index}")
            measure = measure_value.strip() if isinstance(measure_value, str) else ""
            ingredient_pairs.append(
                {"ingredient": ingredient, "measure": measure}
            )

        def clean_optional(field_name: str) -> Optional[str]:
            value = meal.get(field_name)
            if not isinstance(value, str):
                return None
            return value.strip() or None

        return {
            "name": clean_optional("strMeal"),
            "category": clean_optional("strCategory"),
            "area": clean_optional("strArea"),
            "thumbnail": clean_optional("strMealThumb"),
            "ingredients": ingredient_pairs,
            "instructions": clean_optional("strInstructions"),
            "source_url": clean_optional("strSource"),
        }

    @staticmethod
    def _wrap_result(result: Any) -> Dict[str, Any]:
        if isinstance(result, dict) and result.get("status") in {
            "success",
            "user_fixable",
            "system_error",
        }:
            return result
        technical_error = classify_tool_response(result)
        if technical_error:
            return wrap_system_error(
                technical_error["code"],
                technical_error["message"],
                retryable=technical_error["retryable"],
                data=result,
            )
        return wrap_success(result)

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatch a tool and always return the shared response envelope."""
        started = time.perf_counter()
        logger.info("TOOL_START | %s", name)
        dispatch = {
            "find_recipes_by_ingredients": self.find_recipes_by_ingredients,
            "browse_recipes_by_cuisine_or_category": (
                self.browse_recipes_by_cuisine_or_category
            ),
            "get_recipe_details": self.get_recipe_details,
        }
        implementation = dispatch.get(name)
        if implementation is None:
            logger.warning("TOOL_UNKNOWN | %s", name)
            return wrap_system_error(
                "REQUEST_FAILED", f"Unknown tool '{name}'", retryable=False
            )
        try:
            result = await implementation(**(arguments or {}))
            envelope = self._wrap_result(result)
            logger.info(
                "TOOL_END | %s | %.3fs | status=%s",
                name,
                time.perf_counter() - started,
                envelope["status"],
            )
            return envelope
        except (TypeError, ValueError) as exc:
            logger.warning(
                "TOOL_ERROR | %s | %.3fs | %s",
                name,
                time.perf_counter() - started,
                exc,
            )
            return wrap_system_error("INVALID_ARGUMENTS", str(exc), retryable=False)
        except Exception as exc:
            logger.error(
                "TOOL_ERROR | %s | %.3fs | %s",
                name,
                time.perf_counter() - started,
                exc,
                exc_info=True,
            )
            return wrap_system_error("UNEXPECTED_ERROR", str(exc), retryable=True)

    async def handle_request(
        self, request: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Handle MCP-compatible JSON-RPC requests."""
        method = request.get("method")
        params = request.get("params") or {}
        request_id = request.get("id")
        logger.info("FN handle_request | method=%s id=%s", method, request_id)

        server_info = {
            "name": self.config.server_name,
            "version": self.config.version,
        }

        if method == "server/discover":
            # MCP 2026-07-28 discovery result. serverInfo moved into the result
            # metadata in the final specification. The top-level copy is kept as
            # a compatibility field for clients built against the earlier draft.
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "resultType": "complete",
                    "supportedVersions": list(SUPPORTED_MCP_PROTOCOL_VERSIONS),
                    "capabilities": MCP_CAPABILITIES,
                    "_meta": {
                        "io.modelcontextprotocol/serverInfo": server_info,
                    },
                    "serverInfo": server_info,
                    "instructions": (
                        "Use find_recipes_by_ingredients or "
                        "browse_recipes_by_cuisine_or_category before suggesting "
                        "recipes. Use get_recipe_details only after a user selects "
                        "a returned meal. Send only arguments defined by each tool input schema, "
                        "and never include access tokens, passwords, or API keys in "
                        "tool arguments."
                    ),
                    "ttlMs": MCP_DISCOVERY_TTL_MS,
                    "cacheScope": "public",
                },
            }

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": MCP_CAPABILITIES,
                    "serverInfo": server_info,
                },
            }
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": self.tools},
            }
        if method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            result = await self.call_tool(name, arguments)
            text_result = json.dumps(result, ensure_ascii=False)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": text_result}],
                    "structuredContent": result,
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    async def run_stdio(self) -> None:
        """Run a newline-delimited JSON-RPC stdio transport."""
        logger.info("Starting Fridge Chef MCP server over stdio")
        while True:
            line = await asyncio.get_running_loop().run_in_executor(
                None, sys.stdin.readline
            )
            if not line:
                break
            try:
                request = json.loads(line)
                response = await self.handle_request(request)
                if response is not None:
                    print(json.dumps(response, ensure_ascii=False), flush=True)
            except json.JSONDecodeError:
                print(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32700, "message": "Parse error"},
                        }
                    ),
                    flush=True,
                )


async def _main() -> None:
    server = FridgeChefServer()
    try:
        await server.run_stdio()
    finally:
        await server.cleanup()


if __name__ == "__main__":
    asyncio.run(_main())
