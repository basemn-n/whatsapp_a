"""Safety tests for AI-assisted visual product search."""
import unittest

from visual_product_search import select_visual_product_matches


class VisualProductSearchTests(unittest.TestCase):
    def setUp(self):
        self.products = [
            {
                "id": "p1", "name": "عباية سوداء", "price": 12000,
                "currency": "YER", "is_available": True, "stock_quantity": 3,
            },
            {
                "id": "p2", "name": "عباية بيضاء", "price": 13000,
                "currency": "YER", "is_available": True, "stock_quantity": 2,
            },
            {
                "id": "p3", "name": "عباية قديمة", "price": 9000,
                "currency": "YER", "is_available": False, "stock_quantity": 4,
            },
            {
                "id": "p4", "name": "عباية نفدت", "price": 8000,
                "currency": "YER", "is_available": True, "stock_quantity": 0,
            },
        ]

    def test_returns_only_known_available_products_with_confidence(self):
        result = select_visual_product_matches(self.products, {
            "matches": [
                {"product_id": "p2", "confidence": 82, "reason": "لون وشكل قريب"},
                {"product_id": "p1", "confidence": 70, "reason": "تصميم قريب"},
            ]
        })
        self.assertEqual([item["product"]["id"] for item in result], ["p2", "p1"])
        self.assertEqual(result[0]["product"]["price"], 13000)

    def test_rejects_unknown_ids_unavailable_products_and_low_confidence(self):
        result = select_visual_product_matches(self.products, {
            "matches": [
                {"product_id": "foreign-store-product", "confidence": 99},
                {"product_id": "p3", "confidence": 99},
                {"product_id": "p4", "confidence": 99},
                {"product_id": "p1", "confidence": 20},
            ]
        })
        self.assertEqual(result, [])

    def test_caps_results_and_deduplicates_ids(self):
        result = select_visual_product_matches(self.products, {
            "matches": [
                {"product_id": "p1", "confidence": 70},
                {"product_id": "p1", "confidence": 90},
                {"product_id": "p2", "confidence": 80},
            ]
        }, limit=1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["product"]["id"], "p2")

    def test_invalid_model_response_fails_closed(self):
        self.assertEqual(select_visual_product_matches(self.products, []), [])
        self.assertEqual(
            select_visual_product_matches(self.products, {"matches": "not-a-list"}),
            [],
        )


if __name__ == "__main__":
    unittest.main()
