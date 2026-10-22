import unittest

from fastapi import HTTPException

from services.image_count import parse_image_count


class ParseImageCountTest(unittest.TestCase):
    def error(self, value, **kwargs):
        with self.assertRaises(HTTPException) as ctx:
            parse_image_count(value, **kwargs)
        self.assertEqual(ctx.exception.status_code, 400)
        error = ctx.exception.detail["error"]
        self.assertEqual(error["type"], "invalid_request_error")
        self.assertIn("1 and", error["message"])
        return error["code"]

    def test_valid_values(self):
        self.assertEqual(parse_image_count(None), 1)
        self.assertEqual(parse_image_count(""), 1)
        self.assertEqual(parse_image_count("3"), 3)
        self.assertEqual(parse_image_count(4), 4)
        self.assertEqual(parse_image_count(10, maximum=10), 10)

    def test_too_large(self):
        self.assertEqual(self.error(5), "n_too_large")
        self.assertEqual(self.error("11", maximum=10), "n_too_large")

    def test_invalid(self):
        for value in (0, -1, "abc", True, 1.5, [], {}):
            self.assertEqual(self.error(value), "invalid_n", value)


if __name__ == "__main__":
    unittest.main()
