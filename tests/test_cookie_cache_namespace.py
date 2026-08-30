import unittest
from unittest import mock

from gemini_webapi.utils.rotate_1psidts import get_cookie_cache_path


class TestCookieCacheNamespace(unittest.TestCase):
    def test_cache_path_uses_alias_not_raw_session_value(self):
        raw_session_value = "sensitive-session-value"
        with mock.patch.dict("os.environ", {"GEMINI_COOKIE_PATH": "test-cache"}):
            path = get_cookie_cache_path(raw_session_value, account_alias="gemini_01")

        self.assertEqual(path.name, ".cached_cookies_gemini_01.json")
        self.assertNotIn(raw_session_value, str(path))


if __name__ == "__main__":
    unittest.main()
