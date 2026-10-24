"""Unit tests for product selection without importing the live application or database."""
import ast
import unittest
from pathlib import Path


SOURCE = Path("main.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
HELPERS = [
    node for node in TREE.body
    if isinstance(node, ast.FunctionDef)
    and node.name in {"normalize_text", "find_product", "telegram_chat_matches_configured"}
]
NAMESPACE = {}
exec(compile(ast.Module(body=HELPERS, type_ignores=[]), "main.py", "exec"), NAMESPACE)
find_product = NAMESPACE["find_product"]
telegram_chat_matches_configured = NAMESPACE["telegram_chat_matches_configured"]


class TelegramChatAllowlistTests(unittest.TestCase):
    def test_numeric_chat_id_matches_exactly(self):
        self.assertTrue(telegram_chat_matches_configured({"id": -100123, "username": "shop"}, "-100123"))

    def test_username_allowlist_matches_group(self):
        self.assertTrue(telegram_chat_matches_configured({"id": -100123, "username": "shop"}, "@shop"))

    def test_username_matching_is_case_insensitive(self):
        self.assertTrue(telegram_chat_matches_configured({"id": -100123, "username": "Shop"}, "shop"))

    def test_wrong_username_is_rejected(self):
        self.assertFalse(telegram_chat_matches_configured({"id": -100123, "username": "shop"}, "@other"))

    def test_numeric_allowlist_does_not_match_a_different_chat(self):
        self.assertFalse(telegram_chat_matches_configured({"id": -100456, "username": "shop"}, "-100123"))

    def test_empty_allowlist_allows_all_chats_for_mapping_check(self):
        self.assertTrue(telegram_chat_matches_configured({"id": -100123}, ""))

class TelegramProductSelectionTests(unittest.TestCase):
    def setUp(self):
        self.products = [
            {"id": "product-a", "name": "سماعة بلوتث", "model_number": "71"},
            {"id": "product-b", "name": "سماعة بلوتث", "model_number": "72"},
            {"id": "product-c", "name": "عباية كريب فخم", "model_number": "4410"},
        ]

    def test_duplicate_name_does_not_silently_choose_first_product(self):
        self.assertIsNone(find_product(self.products, "سماعة بلوتث"))

    def test_model_number_selects_exact_product(self):
        selected = find_product(self.products, "72")
        self.assertEqual(selected["id"], "product-b")

    def test_unique_product_name_still_selects_product(self):
        selected = find_product(self.products, "عباية كريب فخم")
        self.assertEqual(selected["id"], "product-c")


if __name__ == "__main__":
    unittest.main()
