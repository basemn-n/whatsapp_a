"""Unit tests for product selection without importing the live application or database."""
import ast
import unittest
from pathlib import Path


SOURCE = Path("main.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
HELPERS = [
    node for node in TREE.body
    if isinstance(node, ast.FunctionDef)
    and node.name in {"normalize_text", "find_product"}
]
NAMESPACE = {}
exec(compile(ast.Module(body=HELPERS, type_ignores=[]), "main.py", "exec"), NAMESPACE)
find_product = NAMESPACE["find_product"]


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
