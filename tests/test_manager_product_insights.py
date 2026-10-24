"""Tests for product-level accuracy in the smart store manager."""
import unittest

from admin_dashboard import products_without_recent_deliveries


class ProductsWithoutRecentDeliveriesTests(unittest.TestCase):
    def test_uses_product_id_when_names_are_duplicated(self):
        products = [
            {"id": "p1", "name": "سماعة", "is_available": True},
            {"id": "p2", "name": "سماعة", "is_available": True},
        ]
        delivered_orders = [
            {"product_id": "p2", "product_name": "سماعة", "status": "delivered"}
        ]

        self.assertEqual(
            products_without_recent_deliveries(products, delivered_orders),
            ["سماعة"],
        )

    def test_legacy_order_matches_a_unique_product_name(self):
        products = [
            {"id": "p1", "name": "شاحن", "is_available": True},
            {"id": "p2", "name": "سماعة", "is_available": True},
        ]
        delivered_orders = [
            {"product_name": "شاحن", "status": "delivered"}
        ]

        self.assertEqual(
            products_without_recent_deliveries(products, delivered_orders),
            ["سماعة"],
        )

    def test_ambiguous_legacy_order_does_not_create_false_no_sales_alerts(self):
        products = [
            {"id": "p1", "name": "سماعة", "is_available": True},
            {"id": "p2", "name": "سماعة", "is_available": True},
            {"id": "p3", "name": "شاحن", "is_available": True},
        ]
        delivered_orders = [
            {"product_name": "سماعة", "status": "delivered"}
        ]

        self.assertEqual(
            products_without_recent_deliveries(products, delivered_orders),
            ["شاحن"],
        )

    def test_excludes_unavailable_products_from_slow_product_alerts(self):
        products = [
            {"id": "p1", "name": "غير متاح", "is_available": False},
            {"id": "p2", "name": "متاح", "is_available": True},
        ]

        self.assertEqual(
            products_without_recent_deliveries(products, []),
            ["متاح"],
        )


if __name__ == "__main__":
    unittest.main()
