import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.accounts import platforms  # noqa: E402
from server.accounts.manager import find_chrome, LAUNCH_ARGS, STEALTH_JS  # noqa: E402


class TestPlatforms(unittest.TestCase):
    def test_registry_complete(self):
        ids = set(platforms.all().keys())
        self.assertEqual(
            ids, {"tiktok", "youtube", "facebook", "instagram", "threads"})
        for pid in ids:
            spec = platforms.get(pid)
            self.assertTrue(spec.login_url.startswith("https://"))
            self.assertTrue(spec.upload_url.startswith("https://"))
            self.assertTrue(spec.login_check_js.strip())
            self.assertTrue(len(spec.login_success_hosts) > 0)

    def test_tiktok_check_uses_data_e2e(self):
        js = platforms.get("tiktok").login_check_js
        self.assertIn("data-e2e", js)

    def test_facebook_check_excludes_loginform(self):
        js = platforms.get("facebook").login_check_js
        self.assertIn("loginform", js)

    def test_public_list(self):
        lst = platforms.public_list()
        self.assertEqual(len(lst), 5)
        self.assertTrue(all("schedule_supported" in x for x in lst))


class TestChromeDetect(unittest.TestCase):
    def test_find_chrome(self):
        # on this Windows dev box Chrome may or may not exist; just no-crash
        path = find_chrome()
        if path is not None:
            self.assertTrue(os.path.isfile(path))

    def test_stealth_bits(self):
        self.assertIn("webdriver", STEALTH_JS)
        self.assertIn("AutomationControlled", " ".join(LAUNCH_ARGS))


if __name__ == "__main__":
    unittest.main()
