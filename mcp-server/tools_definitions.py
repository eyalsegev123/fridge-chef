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
                            "minLength": 1,
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
            "name": "get_recipe_details",
            "description": (
                "Use this only after the user selects a meal returned by "
                "find_recipes_by_ingredients. Fetches the exact ingredients, measures, "
                "instructions, image, and source for that meal; do not use it to search "
                "for recipe ideas."
            ),
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "meal_id": {
                        "type": "string",
                        "pattern": "^[0-9]+$",
                        "description": (
                            "The digits-only meal ID returned by "
                            "find_recipes_by_ingredients."
                        ),
                    }
                },
                "required": ["meal_id"],
            },
        },
    ]
