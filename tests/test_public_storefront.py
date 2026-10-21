"""Tests for the public read-only storefront route."""
import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin_dashboard import create_admin_router


BUSINESS_ID = "68194d49-7ba4-4f49-867e-338b048d63e1"


class FakeQuery:
    def __init__(self, table):
        self.table = table

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def execute(self):
        if self.table == "businesses":
            return SimpleNamespace(data=[{
                "id": BUSINESS_ID,
                "name": "متجر <تجريبي>",
                "industry": "تجارة",
                "description": "وصف المتجر",
            }])
        if self.table == "products":
            return SimpleNamespace(data=[{
                "name": "سماعة",
                "description": "منتج متاح",
                "price": 6000,
                "currency": "YER",
                "model_number": "71",
                "category": "إلكترونيات",
                "stock_quantity": 10,
                "is_available": True,
            }])
        return SimpleNamespace(data=[])


class FakeSupabase:
    def table(self, table):
        return FakeQuery(table)


class PublicStorefrontTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(create_admin_router(FakeSupabase()))
        cls.client = TestClient(app)

    def test_storefront_is_public_and_renders_available_products(self):
        response = self.client.get(f"/store/{BUSINESS_ID}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("سماعة", response.text)
        self.assertIn("6000", response.text)
        self.assertIn("اسأل أو اطلب عبر Telegram", response.text)
        self.assertIn(f"https://t.me/SmartCustomerHelp2026Bot?start={BUSINESS_ID}", response.text)

    def test_storefront_escapes_business_content(self):
        response = self.client.get(f"/store/{BUSINESS_ID}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("&lt;تجريبي&gt;", response.text)
        self.assertNotIn("<تجريبي>", response.text)

    def test_invalid_business_id_is_not_found(self):
        response = self.client.get("/store/not-a-uuid")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
