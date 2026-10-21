"""Tests for privacy-preserving AI token metering helpers."""
import ast
import unittest
import uuid
from pathlib import Path


SOURCE = Path("bot_logic.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
HELPERS = [
    node for node in TREE.body
    if isinstance(node, ast.FunctionDef)
    and node.name in {"extract_token_usage", "_is_uuid"}
]
NAMESPACE = {"uuid": uuid}
exec(compile(ast.Module(body=HELPERS, type_ignores=[]), "bot_logic.py", "exec"), NAMESPACE)
extract_token_usage = NAMESPACE["extract_token_usage"]
is_uuid = NAMESPACE["_is_uuid"]


class AIUsageHelperTests(unittest.TestCase):
    def test_extracts_langchain_usage_metadata(self):
        response = type("Response", (), {
            "usage_metadata": {
                "input_tokens": 120,
                "output_tokens": 30,
                "total_tokens": 150,
            },
            "response_metadata": {},
        })()
        self.assertEqual(
            extract_token_usage(response),
            {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
        )

    def test_supports_openai_compatible_token_usage(self):
        response = type("Response", (), {
            "usage_metadata": None,
            "response_metadata": {
                "token_usage": {
                    "prompt_tokens": 21,
                    "completion_tokens": 9,
                    "total_tokens": 30,
                }
            },
        })()
        self.assertEqual(
            extract_token_usage(response),
            {"input_tokens": 21, "output_tokens": 9, "total_tokens": 30},
        )

    def test_missing_or_invalid_usage_is_zero_not_negative(self):
        response = type("Response", (), {
            "usage_metadata": {"input_tokens": -1, "output_tokens": True},
            "response_metadata": {},
        })()
        self.assertEqual(
            extract_token_usage(response),
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )

    def test_business_id_must_be_uuid(self):
        self.assertTrue(is_uuid("68194d49-7ba4-4f49-867e-338b048d63e1"))
        self.assertFalse(is_uuid("telegram_123_business"))
        self.assertFalse(is_uuid(None))


if __name__ == "__main__":
    unittest.main()
