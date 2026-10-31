import unittest

from rangeset import Range, merge, parse, ParseError, format_ranges, compress, total_length


class TestBasic(unittest.TestCase):
    def test_parse_simple(self):
        self.assertEqual(parse("1-3,5"), [Range(1, 3), Range(5, 5)])

    def test_parse_empty_and_blank_items(self):
        self.assertEqual(parse(""), [])
        self.assertEqual(parse("1,,2"), [Range(1, 1), Range(2, 2)])

    def test_parse_bad(self):
        with self.assertRaises(ParseError):
            parse("1-x")
        with self.assertRaises(ParseError):
            parse("5-1")

    def test_merge_overlap_and_adjacent(self):
        self.assertEqual(merge([Range(1, 3), Range(2, 6)]), [Range(1, 6)])
        self.assertEqual(merge([Range(4, 6), Range(1, 3)]), [Range(1, 6)])
        self.assertEqual(merge([Range(1, 2), Range(5, 6)]), [Range(1, 2), Range(5, 6)])

    def test_format_and_compress(self):
        self.assertEqual(format_ranges([Range(1, 3), Range(5, 5)]), "1-3,5")
        self.assertEqual(compress("10-12,1-3,4"), "1-4,10-12")

    def test_total_length(self):
        self.assertEqual(total_length([Range(1, 3), Range(2, 4)]), 4)


if __name__ == "__main__":
    unittest.main()
