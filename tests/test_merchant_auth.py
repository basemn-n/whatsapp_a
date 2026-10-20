"""Unit tests for merchant session token and password helpers.

Run from the repository root with:
    python -m unittest discover -s tests -v
"""
import base64
import json
import os
import time
import unittest
from unittest.mock import patch

import admin_dashboard


def encode_payload(payload):
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def signed_token(payload, secret):
    encoded = encode_payload(payload)
    import hashlib
    import hmac

    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    return encoded + "." + base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")


class MerchantTokenTests(unittest.TestCase):
    def setUp(self):
        self.secret = "unit-test-secret-not-for-production"
        self.valid_payload = {
            "username": "merchant_one",
            "business_id": "business-uuid-1",
            "exp": int(time.time()) + 300,
        }
        self.secret_patch = patch.dict(os.environ, {"MERCHANT_SESSION_SECRET": self.secret})
        self.secret_patch.start()
        self.addCleanup(self.secret_patch.stop)

    def test_accepts_valid_signed_token(self):
        token = signed_token(self.valid_payload, self.secret)
        self.assertEqual(admin_dashboard._read_merchant_token(token), self.valid_payload)

    def test_rejects_expired_token(self):
        payload = dict(self.valid_payload, exp=int(time.time()) - 1)
        self.assertIsNone(admin_dashboard._read_merchant_token(signed_token(payload, self.secret)))

    def test_rejects_token_without_identity(self):
        payload = {"exp": int(time.time()) + 300, "username": "merchant_one"}
        self.assertIsNone(admin_dashboard._read_merchant_token(signed_token(payload, self.secret)))

    def test_rejects_non_integer_expiration(self):
        payload = dict(self.valid_payload, exp=str(int(time.time()) + 300))
        self.assertIsNone(admin_dashboard._read_merchant_token(signed_token(payload, self.secret)))

    def test_rejects_boolean_expiration(self):
        payload = dict(self.valid_payload, exp=True)
        self.assertIsNone(admin_dashboard._read_merchant_token(signed_token(payload, self.secret)))

    def test_rejects_modified_signature(self):
        token = signed_token(self.valid_payload, self.secret)
        encoded, signature = token.split(".", 1)
        altered = ("A" if signature[:1] != "A" else "B") + signature[1:]
        self.assertIsNone(admin_dashboard._read_merchant_token(encoded + "." + altered))

    def test_rejects_malformed_token(self):
        self.assertIsNone(admin_dashboard._read_merchant_token("not-a-valid-token"))
        self.assertIsNone(admin_dashboard._read_merchant_token("a.b.c"))

    def test_rejects_token_when_secret_missing(self):
        with patch.dict(os.environ, {"MERCHANT_SESSION_SECRET": ""}):
            self.assertIsNone(admin_dashboard._read_merchant_token(signed_token(self.valid_payload, self.secret)))


class MerchantPasswordTests(unittest.TestCase):
    def test_password_hash_matches_only_original_password(self):
        encoded = admin_dashboard._password_hash("correct-horse-battery-staple")
        self.assertTrue(admin_dashboard._password_matches("correct-horse-battery-staple", encoded))
        self.assertFalse(admin_dashboard._password_matches("wrong-password", encoded))

    def test_password_match_rejects_malformed_hash(self):
        self.assertFalse(admin_dashboard._password_matches("any-password", "malformed"))


if __name__ == "__main__":
    unittest.main()
