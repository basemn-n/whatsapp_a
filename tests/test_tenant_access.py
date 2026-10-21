"""Tests for merchant-to-business authorization boundaries."""
import unittest

from fastapi import HTTPException

from admin_dashboard import ensure_business_access


class BusinessAccessTests(unittest.TestCase):
    def test_merchant_can_access_own_business(self):
        ensure_business_access("business-a", {
            "role": "merchant",
            "business_id": "business-a",
        })

    def test_merchant_cannot_access_another_business(self):
        with self.assertRaises(HTTPException) as caught:
            ensure_business_access("business-b", {
                "role": "merchant",
                "business_id": "business-a",
            })
        self.assertEqual(caught.exception.status_code, 403)

    def test_merchant_without_business_is_denied(self):
        with self.assertRaises(HTTPException) as caught:
            ensure_business_access("business-a", {
                "role": "merchant",
                "business_id": None,
            })
        self.assertEqual(caught.exception.status_code, 403)

    def test_admin_can_access_any_business(self):
        ensure_business_access("business-b", {
            "role": "admin",
            "business_id": None,
        })


if __name__ == "__main__":
    unittest.main()
