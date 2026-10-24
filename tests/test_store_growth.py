import unittest

from store_growth import (
    extract_topic,
    format_loyalty_summary,
    is_product_inquiry,
    is_shopping_advice_request,
)


class StoreGrowthHelperTests(unittest.TestCase):
    def test_detects_advice_intent(self):
        self.assertTrue(is_shopping_advice_request("أريد هدية بميزانية 10000 ريال"))
        self.assertTrue(is_shopping_advice_request("قارن بين هذه المنتجات"))
        self.assertFalse(is_shopping_advice_request("أريد طلب سماعة بلوتث"))

    def test_detects_product_inquiry(self):
        self.assertTrue(is_product_inquiry("بكم العباية السوداء؟"))
        self.assertFalse(is_product_inquiry("السلام عليكم"))

    def test_topic_uses_only_catalog_names_or_neutral_label(self):
        products = [{"name": "عباية سوداء", "model_number": "3325"}]
        self.assertEqual(extract_topic("كم سعر عباية سوداء؟", products), "عباية سوداء")
        self.assertEqual(extract_topic("أريد هدية", []), "الهدية التي تبحث عنها")
        self.assertEqual(extract_topic("السلام عليكم", []), "المنتج الذي سألت عنه")

    def test_loyalty_summary_is_clear(self):
        text = format_loyalty_summary(20, 50, 100, 1)
        self.assertIn("20", text)
        self.assertIn("100", text)


if __name__ == "__main__":
    unittest.main()
