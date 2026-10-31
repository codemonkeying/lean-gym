import unittest

from rangeset import Range, merge, parse, ParseError, format_ranges, compress, contains


class TestHiddenP2P(unittest.TestCase):
    def test_merge_empty(self):
        self.assertEqual(merge([]), [])

    def test_merge_adjacent_and_gap(self):
        self.assertEqual(merge([Range(1, 3), Range(4, 6), Range(8, 9)]),
                         [Range(1, 6), Range(8, 9)])

    def test_merge_does_not_mutate_input(self):
        src = [Range(5, 6), Range(1, 2)]
        merge(src)
        self.assertEqual(src, [Range(5, 6), Range(1, 2)])

    def test_merge_partial_overlap_unsorted(self):
        self.assertEqual(merge([Range(5, 9), Range(1, 6), Range(-3, -1)]),
                         [Range(-3, -1), Range(1, 9)])

    def test_parse_spaces_and_negatives(self):
        self.assertEqual(parse(" 1 - 3 , -2 "), [Range(1, 3), Range(-2, -2)])
        with self.assertRaises(ParseError):
            parse("a")

    def test_format_sep_and_compress(self):
        self.assertEqual(format_ranges([Range(1, 1), Range(3, 4)], sep=";"), "1;3-4")
        self.assertEqual(compress("7,5,6,1-2"), "1-2,5-7")

    def test_contains_and_range_errors(self):
        self.assertTrue(contains([Range(1, 3)], 2))
        self.assertFalse(contains([Range(1, 3)], 4))
        with self.assertRaises(ValueError):
            Range(3, 1)

    def test_merge_duplicate_ranges(self):
        self.assertEqual(merge([Range(1, 3), Range(1, 3)]), [Range(1, 3)])

    def test_merge_same_start_shorter_first_after_sort(self):
        self.assertEqual(merge([Range(1, 5), Range(1, 2)]), [Range(1, 5)])

    def test_contains_nested_input(self):
        rs = [Range(1, 10), Range(3, 4)]
        self.assertTrue(contains(rs, 7) and contains(rs, 3))
        self.assertFalse(contains(rs, 11))


if __name__ == "__main__":
    unittest.main()
