"""Tests for clearing an unfinished Telegram order when a customer changes stores."""
import ast
import unittest
from pathlib import Path


SOURCE = Path("main.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
HELPER = next(
    node for node in TREE.body
    if isinstance(node, ast.FunctionDef)
    and node.name == "clear_pending_order_if_store_changed"
)


class TelegramStoreSwitchTests(unittest.TestCase):
    def run_helper(self, state, requested_business_id):
        calls = {"cleared": 0, "loaded_chat_id": None}

        def load_pending_telegram_order(chat_id):
            calls["loaded_chat_id"] = chat_id
            return state

        def clear_pending_telegram_order(chat_id):
            calls["cleared"] += 1

        namespace = {
            "load_pending_telegram_order": load_pending_telegram_order,
            "clear_pending_telegram_order": clear_pending_telegram_order,
        }
        exec(
            compile(ast.Module(body=[HELPER], type_ignores=[]), "main.py", "exec"),
            namespace,
        )
        result = namespace["clear_pending_order_if_store_changed"](
            12345, requested_business_id
        )
        return result, calls

    def test_keeps_pending_order_when_store_is_unchanged(self):
        result, calls = self.run_helper(
            {"business_id": "store-a", "stage": "customer_name"}, "store-a"
        )
        self.assertFalse(result)
        self.assertEqual(calls["cleared"], 0)

    def test_clears_pending_order_when_store_changes(self):
        result, calls = self.run_helper(
            {"business_id": "store-a", "stage": "customer_name"}, "store-b"
        )
        self.assertTrue(result)
        self.assertEqual(calls["cleared"], 1)

    def test_clears_malformed_pending_state_without_store_id(self):
        result, calls = self.run_helper(
            {"stage": "customer_name"}, "store-b"
        )
        self.assertTrue(result)
        self.assertEqual(calls["cleared"], 1)

    def test_does_not_clear_when_there_is_no_pending_order(self):
        result, calls = self.run_helper(None, "store-b")
        self.assertFalse(result)
        self.assertEqual(calls["cleared"], 0)


if __name__ == "__main__":
    unittest.main()
