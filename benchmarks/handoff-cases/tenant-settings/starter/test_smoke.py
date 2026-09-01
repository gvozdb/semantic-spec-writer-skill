import unittest

from solution import resolve_setting


class SettingSmokeTest(unittest.TestCase):
    def test_request_precedence_and_integer_coercion(self):
        result = resolve_setting(
            "limit",
            "int",
            1,
            {"limit": " 7 "},
            {"limit": 5},
            {"limit": 3},
        )
        self.assertEqual(result, {"name": "limit", "value": 7, "source": "request"})


if __name__ == "__main__":
    unittest.main()
