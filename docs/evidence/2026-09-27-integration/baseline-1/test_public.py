import unittest
from solution import parse_duration


class Public(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(parse_duration("1h30m15s"), 5415)

    def test_invalid(self):
        with self.assertRaises(ValueError):
            parse_duration("bad")
