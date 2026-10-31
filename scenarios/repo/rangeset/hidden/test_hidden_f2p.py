import unittest

from rangeset import Range, merge, compress, total_length


class TestHiddenF2P(unittest.TestCase):
    def test_merge_nested_range(self):
        self.assertEqual(merge([Range(1, 10), Range(3, 4)]), [Range(1, 10)])

    def test_merge_nested_then_more(self):
        self.assertEqual(
            merge([Range(1, 10), Range(2, 3), Range(11, 12), Range(20, 25), Range(21, 21)]),
            [Range(1, 12), Range(20, 25)])

    def test_compress_nested(self):
        self.assertEqual(compress("1-10,3-4"), "1-10")

    def test_total_length_nested(self):
        self.assertEqual(total_length([Range(1, 10), Range(2, 3)]), 10)

    def test_merge_nested_then_adjacent(self):
        self.assertEqual(merge([Range(1, 10), Range(2, 3), Range(11, 11)]), [Range(1, 11)])


if __name__ == "__main__":
    unittest.main()
