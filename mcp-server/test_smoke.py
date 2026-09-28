"""Network-isolated smoke tests for the Fridge Chef MCP tools.

Run with: python -m unittest -v test_smoke.py
"""

import json
import unittest
from typing import Any, Callable, Dict

import httpx

from server_simple import FridgeChefServer


BackendHandler = Callable[[httpx.Request], httpx.Response]


class FridgeChefSmokeTests(unittest.IsolatedAsyncioTestCase):
    """Exercise tool behavior through the public JSON-RPC request handler."""

    async def asyncSetUp(self) -> None:
        self.server = FridgeChefServer("config.json")

    async def asyncTearDown(self) -> None:
        await self.server.cleanup()

    def use_backend(self, handler: BackendHandler) -> None:
        self.server.http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )

    async def call_tool(
        self, name: str, arguments: Dict[str, Any], request_id: int = 1
    ) -> Dict[str, Any]:
        response = await self.server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        self.assertIsNotNone(response)
        assert response is not None
        structured = response["result"]["structuredContent"]
        self.assertEqual(
            json.loads(response["result"]["content"][0]["text"]), structured
        )
        return structured

    async def test_multi_ingredient_search_returns_exact_matches(self) -> None:
        responses = {
            "chicken": [
                {
                    "idMeal": "100",
                    "strMeal": "Chicken and Rice",
                    "strMealThumb": "https://images.example/100.jpg",
                },
                {
                    "idMeal": "101",
                    "strMeal": "Chicken Soup",
                    "strMealThumb": "https://images.example/101.jpg",
                },
            ],
            "rice": [
                {
                    "idMeal": "100",
                    "strMeal": "Chicken and Rice",
                    "strMealThumb": "https://images.example/100.jpg",
                },
                {
                    "idMeal": "102",
                    "strMeal": "Rice Pudding",
                    "strMealThumb": "https://images.example/102.jpg",
                },
            ],
        }
        requested_ingredients = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.path.rsplit("/", 1)[-1], "filter.php")
            ingredient = request.url.params["i"]
            requested_ingredients.append(ingredient)
            return httpx.Response(200, json={"meals": responses[ingredient]})

        self.use_backend(handler)
        envelope = await self.call_tool(
            "find_recipes_by_ingredients",
            {"ingredients": ["chicken", "rice"]},
        )

        self.assertEqual(envelope["status"], "success")
        self.assertIsNone(envelope["error"])
        self.assertFalse(envelope["data"]["partial_match"])
        self.assertCountEqual(requested_ingredients, ["chicken", "rice"])
        self.assertEqual(
            envelope["data"]["meals"],
            [
                {
                    "meal_id": "100",
                    "name": "Chicken and Rice",
                    "thumbnail": "https://images.example/100.jpg",
                    "matched_count": 2,
                    "matched_ingredients": ["chicken", "rice"],
                }
            ],
        )

    async def test_empty_intersection_returns_ranked_partial_matches(self) -> None:
        responses = {
            "chicken": [
                {
                    "idMeal": "200",
                    "strMeal": "Shared Favorite",
                    "strMealThumb": "https://images.example/200.jpg",
                },
                {
                    "idMeal": "201",
                    "strMeal": "Chicken Only",
                    "strMealThumb": "https://images.example/201.jpg",
                },
            ],
            "rice": [
                {
                    "idMeal": "200",
                    "strMeal": "Shared Favorite",
                    "strMealThumb": "https://images.example/200.jpg",
                },
                {
                    "idMeal": "202",
                    "strMeal": "Rice Only",
                    "strMealThumb": "https://images.example/202.jpg",
                },
            ],
            "tomato": [
                {
                    "idMeal": "203",
                    "strMeal": "Tomato Only",
                    "strMealThumb": "https://images.example/203.jpg",
                }
            ],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"meals": responses[request.url.params["i"]]}
            )

        self.use_backend(handler)
        envelope = await self.call_tool(
            "find_recipes_by_ingredients",
            {"ingredients": ["chicken", "rice", "tomato"]},
        )

        self.assertEqual(envelope["status"], "success")
        self.assertTrue(envelope["data"]["partial_match"])
        meals = envelope["data"]["meals"]
        self.assertEqual(meals[0]["meal_id"], "200")
        self.assertEqual(meals[0]["matched_count"], 2)
        self.assertEqual(meals[0]["matched_ingredients"], ["chicken", "rice"])
        self.assertEqual(meals[0]["unmatched_ingredients"], ["tomato"])
        self.assertEqual([meal["matched_count"] for meal in meals], [2, 1, 1, 1])

    async def test_unrecognized_ingredient_is_user_fixable(self) -> None:
        self.use_backend(lambda _request: httpx.Response(200, json={"meals": None}))

        envelope = await self.call_tool(
            "find_recipes_by_ingredients", {"ingredients": ["notafood"]}
        )

        self.assertEqual(envelope["status"], "user_fixable")
        self.assertEqual(envelope["error"]["code"], "INGREDIENTS_NOT_RECOGNIZED")
        self.assertTrue(envelope["error"]["retryable"])
        self.assertEqual(
            envelope["data"]["unrecognized_ingredients"], ["notafood"]
        )
        self.assertIn("simpler", envelope["error"]["message"].lower())

    async def test_unknown_meal_id_is_user_fixable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path.rsplit("/", 1)[-1], "lookup.php")
            self.assertEqual(request.url.params["i"], "999999")
            return httpx.Response(200, json={"meals": None})

        self.use_backend(handler)
        envelope = await self.call_tool(
            "get_recipe_details", {"meal_id": "999999"}
        )

        self.assertEqual(envelope["status"], "user_fixable")
        self.assertEqual(envelope["error"]["code"], "MEAL_NOT_FOUND")
        self.assertEqual(envelope["data"], {"meal_id": "999999"})

    async def test_recipe_details_collapse_ingredient_measure_pairs(self) -> None:
        meal = {
            "idMeal": "52772",
            "strMeal": "  Test Casserole  ",
            "strCategory": " Chicken ",
            "strArea": " Japanese ",
            "strMealThumb": " https://images.example/52772.jpg ",
            "strInstructions": " Cook carefully. ",
            "strSource": "   ",
            "strIngredient1": "  Chicken breast ",
            "strMeasure1": " 2 pieces ",
            "strIngredient2": "",
            "strMeasure2": "ignored",
            "strIngredient3": None,
            "strMeasure3": None,
            "strIngredient4": " Rice ",
            "strMeasure4": None,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params["i"], "52772")
            return httpx.Response(200, json={"meals": [meal]})

        self.use_backend(handler)
        envelope = await self.call_tool(
            "get_recipe_details", {"meal_id": "52772"}
        )

        self.assertEqual(envelope["status"], "success")
        self.assertEqual(
            envelope["data"],
            {
                "name": "Test Casserole",
                "category": "Chicken",
                "area": "Japanese",
                "thumbnail": "https://images.example/52772.jpg",
                "ingredients": [
                    {"ingredient": "Chicken breast", "measure": "2 pieces"},
                    {"ingredient": "Rice", "measure": ""},
                ],
                "instructions": "Cook carefully.",
                "source_url": None,
            },
        )

    async def test_tools_list_exposes_only_fridge_chef_tools(self) -> None:
        response = await self.server.handle_request(
            {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}
        )
        self.assertIsNotNone(response)
        assert response is not None
        tools = response["result"]["tools"]
        self.assertEqual(
            [tool["name"] for tool in tools],
            ["find_recipes_by_ingredients", "get_recipe_details"],
        )
        self.assertFalse(tools[0]["inputSchema"]["additionalProperties"])
        self.assertEqual(
            tools[0]["inputSchema"]["properties"]["ingredients"]["maxItems"], 5
        )
        self.assertEqual(
            tools[1]["inputSchema"]["properties"]["meal_id"]["pattern"],
            "^[0-9]+$",
        )


if __name__ == "__main__":
    unittest.main()
