"""Route-level tests for merchant tenant isolation.

These tests use a fake Supabase client and never touch production data.
"""
import os
import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin_dashboard import _issue_merchant_token, create_admin_router


MERCHANT_BUSINESS_ID = "business-a"
OTHER_BUSINESS_ID = "business-b"


class FakeQuery:
    def __init__(self, table_name):
        self.table_name = table_name

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def execute(self):
        if self.table_name == "merchant_accounts":
            rows = [{
                "username": "merchant-a",
                "business_id": MERCHANT_BUSINESS_ID,
                "is_active": True,
            }]
        elif self.table_name == "businesses":
            rows = [{"id": MERCHANT_BUSINESS_ID, "name": "متجر تجريبي"}]
        else:
            rows = []
        return SimpleNamespace(data=rows)


class FakeSupabase:
    def table(self, table_name):
        return FakeQuery(table_name)


class MerchantTenantRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["MERCHANT_SESSION_SECRET"] = "test-only-secret-never-use-in-production"
        os.environ["ADMIN_USERNAME"] = "test-admin"
        os.environ["ADMIN_PASSWORD"] = "test-only-admin-password"
        app = FastAPI()
        app.include_router(create_admin_router(FakeSupabase()))
        cls.client = TestClient(app)
        cls.token = _issue_merchant_token("merchant-a", MERCHANT_BUSINESS_ID)
        cls.headers = {"Authorization": f"Bearer {cls.token}"}

    def test_merchant_can_read_own_products(self):
        response = self.client.get(
            "/api/admin/products",
            params={"business_id": MERCHANT_BUSINESS_ID},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"products": []})

    def test_merchant_cannot_read_another_business_products(self):
        response = self.client.get(
            "/api/admin/products",
            params={"business_id": OTHER_BUSINESS_ID},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 403)

    def test_merchant_cannot_create_product_for_another_business(self):
        response = self.client.post(
            "/api/admin/products",
            headers=self.headers,
            json={
                "business_id": OTHER_BUSINESS_ID,
                "name": "منتج تجريبي",
                "price": 100,
                "is_available": True,
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_merchant_cannot_update_another_business_product(self):
        response = self.client.patch(
            "/api/admin/products/product-123",
            params={"business_id": OTHER_BUSINESS_ID},
            headers=self.headers,
            json={
                "name": "منتج معدل",
                "price": 100,
                "is_available": True,
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_merchant_cannot_read_another_business_orders(self):
        response = self.client.get(
            "/api/admin/orders",
            params={"business_id": OTHER_BUSINESS_ID},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 403)

    def test_merchant_cannot_read_another_business_sales_report(self):
        response = self.client.get(
            "/api/admin/reports/sales",
            params={"business_id": OTHER_BUSINESS_ID},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 403)

    def test_merchant_cannot_change_another_business_order_status(self):
        response = self.client.patch(
            "/api/admin/orders/order-123/status",
            params={"business_id": OTHER_BUSINESS_ID},
            headers=self.headers,
            json={"status": "cancelled"},
        )
        self.assertEqual(response.status_code, 403)


    def test_merchant_business_list_is_limited_to_own_business(self):
        response = self.client.get("/api/admin/businesses", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        businesses = response.json()["businesses"]
        self.assertEqual([item["id"] for item in businesses], [MERCHANT_BUSINESS_ID])

    def test_merchant_cannot_list_signup_requests(self):
        response = self.client.get(
            "/api/admin/merchant-signup-requests",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 403)

    def test_merchant_cannot_review_signup_requests(self):
        response = self.client.patch(
            "/api/admin/merchant-signup-requests/request-123",
            headers=self.headers,
            json={"action": "reject", "reason": "test"},
        )
        self.assertEqual(response.status_code, 403)

    def test_merchant_cannot_create_merchant_accounts(self):
        response = self.client.post(
            "/api/admin/merchant-accounts",
            headers=self.headers,
            json={
                "business_id": MERCHANT_BUSINESS_ID,
                "username": "merchant-b",
                "password": "test-only-long-password",
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_products_api_requires_authentication(self):
        response = self.client.get(
            "/api/admin/products",
            params={"business_id": MERCHANT_BUSINESS_ID},
        )
        self.assertEqual(response.status_code, 401)

    def test_invalid_bearer_token_is_rejected(self):
        response = self.client.get(
            "/api/admin/products",
            params={"business_id": MERCHANT_BUSINESS_ID},
            headers={"Authorization": "Bearer invalid-token"},
        )
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
