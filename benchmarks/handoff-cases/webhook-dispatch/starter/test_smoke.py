import hashlib
import hmac
import unittest

from solution import handle_webhook


class WebhookSmokeTest(unittest.TestCase):
    def test_supported_event_is_normalized(self):
        secret = "key"
        body = "hello"
        signature = "sha256=" + hmac.new(
            secret.encode(), body.encode(), hashlib.sha256
        ).hexdigest()
        result = handle_webhook(
            secret,
            body,
            signature,
            {"id": " evt-1 ", "type": " invoice.paid "},
            [],
        )
        self.assertEqual(
            result["delivery"],
            {"event_id": "evt-1", "type": "invoice.paid"},
        )


if __name__ == "__main__":
    unittest.main()
