"""Tests for parse.parse (stdlib unittest; run: python3 tests/test_parse.py)."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))  # repo root
_sys.path[1:1] = [_os.path.join(_sys.path[0], "probes"), _os.path.join(_sys.path[0], "sandbox")]
import unittest

from parse import parse


class TestParse(unittest.TestCase):
    def test_parse_valid_string(self):
        """Test parsing valid string inputs."""
        self.assertEqual(parse("123"), 123)
        self.assertEqual(parse("0"), 0)
        self.assertEqual(parse("-456"), -456)

    def test_parse_invalid_type(self):
        """Test parsing invalid input types."""
        with self.assertRaises(TypeError):
            parse(123)
        with self.assertRaises(TypeError):
            parse(None)
        with self.assertRaises(TypeError):
            parse([])

    def test_parse_empty_string(self):
        """Test parsing empty string."""
        with self.assertRaises(ValueError):
            parse("")

    def test_parse_whitespace_only_string(self):
        """Test parsing whitespace-only strings."""
        with self.assertRaises(ValueError):
            parse("   ")
        with self.assertRaises(ValueError):
            parse("\t\n")

    def test_parse_invalid_string(self):
        """Test parsing invalid string inputs."""
        with self.assertRaises(ValueError):
            parse("abc")
        with self.assertRaises(ValueError):
            parse("12.34")
        with self.assertRaises(ValueError):
            parse("12a34")


if __name__ == "__main__":
    unittest.main()
