"""Model-facing MCP tool schemas.

Keep schemas here and implementations in ``server_simple.py``.
"""

from typing import Any, Dict, List


def get_available_tools() -> List[Dict[str, Any]]:
    """Return the tools exposed by the Fridge Chef MCP server."""
    return [
        {
            "name": "find_recipes_by_ingredients",
            "description": (
                "Use this first whenever the user wants recipe ideas from ingredients "
                "they have. Supply one to five simple, lowercase ingredient names. The "
                "tool finds recipes matching every ingredient when possible and clearly "
                "labels ranked partial matches when no recipe covers them all."
            ),
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "ingredients": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            # No minLength: Azure/OpenAI strict function schemas
                            # do not accept it, and the pattern already requires
                            # at least one letter.
                            "pattern": "^[a-z]+(?: [a-z]+)*$",
                            "description": (
                                "One simple lowercase ingredient, without quantities, "
                                "punctuation, or multiple ingredients in one string."
                            ),
                        },
                        "minItems": 1,
                        "maxItems": 5,
                        "description": (
                            "Simple lowercase ingredients to match, such as "
                            "['chicken', 'rice']."
                        ),
                    }
                },
                "required": ["ingredients"],
            },
        },
        {
            "name": "browse_recipes_by_cuisine_or_category",
            "description": (
                "Use this when the user asks for a cuisine (such as Italian or "
                "Indian) or a kind of dish (such as Dessert, Vegetarian, Seafood) "
                "instead of listing ingredients. Set cuisine, category, or both; "
                "pass null for the one the user did not mention. If a value is not "
                "recognized, the result lists the valid options."
            ),
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "cuisine": {
                        # Strict function calling requires every property to be
                        # listed in "required", so optional values are nullable.
                        "type": ["string", "null"],
                        "description": (
                            "A cuisine or country name such as 'Italian', "
                            "'Mexican' or 'Japanese', or null."
                        ),
                    },
                    "category": {
                        "type": ["string", "null"],
                        "description": (
                            "A dish category such as 'Dessert', 'Vegetarian', "
                            "'Seafood' or 'Pasta', or null."
                        ),
                    },
                },
                "required": ["cuisine", "category"],
            },
        },
        {
            "name": "get_recipe_details",
            "description": (
                "Use this only after the user selects a meal returned by "
                "find_recipes_by_ingredients or "
                "browse_recipes_by_cuisine_or_category. Fetches the exact "
                "ingredients, measures, instructions, image, and source for that "
                "meal; do not use it to search for recipe ideas."
            ),
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "meal_id": {
                        "type": "string",
                        "pattern": "^[0-9]+$",
                        "description": (
                            "The digits-only meal ID returned by a search tool."
                        ),
                    }
                },
                "required": ["meal_id"],
            },
        },
    ]
