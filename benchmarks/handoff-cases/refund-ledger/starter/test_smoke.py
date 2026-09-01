import unittest

from solution import execute_refund


class RefundSmokeTest(unittest.TestCase):
    def test_new_refund_uses_order_id_and_preserves_input(self):
        order = {
            "id": "o1",
            "account_id": "a1",
            "status": "paid",
            "paid_cents": 500,
            "refunded_cents": 0,
        }
        result = execute_refund(
            order,
            {"key": " k ", "refund_id": " r1 ", "amount_cents": 100, "reason": " x "},
            [],
            [],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["refund"]["order_id"], "o1")
        self.assertEqual(order["refunded_cents"], 0)


if __name__ == "__main__":
    unittest.main()
