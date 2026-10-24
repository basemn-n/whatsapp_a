"""Regression tests for atomic Telegram order price snapshots.

The application must not read a product price before the atomic RPC and then
write a second, potentially stale total_price after the order has been created.
"""
import ast
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace


SOURCE = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
FUNCTION = next(
    node for node in TREE.body
    if isinstance(node, ast.FunctionDef) and node.name == "save_telegram_order"
)


class FakeQuery:
    def __init__(self, table_name, client):
        self.table_name = table_name
        self.client = client

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def execute(self):
        self.client.executed_tables.append(self.table_name)
        if self.table_name == "businesses":
            return SimpleNamespace(data=[{"id": "business-a"}])
        raise AssertionError(
            f"Unexpected direct table query during order creation: {self.table_name}"
        )


class FakeRpc:
    def __init__(self, client, name, params):
        self.client = client
        self.name = name
        self.params = params

    def execute(self):
        self.client.rpc_calls.append((self.name, self.params))
        return SimpleNamespace(data={
            "success": True,
            "order": {"order_number": 27, "status": "pending"},
        })


class FakeSupabase:
    def __init__(self):
        self.executed_tables = []
        self.rpc_calls = []

    def table(self, table_name):
        return FakeQuery(table_name, self)

    def rpc(self, name, params):
        return FakeRpc(self, name, params)


class AtomicOrderPriceSnapshotTests(unittest.TestCase):
    def test_order_creation_uses_atomic_rpc_without_secondary_price_write(self):
        fake_supabase = FakeSupabase()
        namespace = {
            "supabase": fake_supabase,
            "parse_quantity": lambda value: int(value) if str(value).isdigit() else None,
            "logger": logging.getLogger("test_atomic_order_price_snapshot"),
        }
        module = ast.Module(body=[FUNCTION], type_ignores=[])
        exec(compile(module, "main.py", "exec"), namespace)

        result = namespace["save_telegram_order"](
            12345,
            {
                "business_id": "business-a",
                "customer_name": "Customer",
                "customer_phone": "123456789",
                "customer_address": "Test address",
                "payment_method": "cash",
                "product_name": "Product",
                "product_id": "product-a",
                "quantity": 2,
            },
        )

        self.assertEqual(result, {"order_number": 27, "status": "pending"})
        self.assertEqual(fake_supabase.executed_tables, ["businesses"])
        self.assertEqual(len(fake_supabase.rpc_calls), 1)
        rpc_name, params = fake_supabase.rpc_calls[0]
        self.assertEqual(rpc_name, "create_telegram_order_with_stock")
        self.assertEqual(params["p_product_id"], "product-a")
        self.assertEqual(params["p_quantity"], 2)


if __name__ == "__main__":
    unittest.main()
