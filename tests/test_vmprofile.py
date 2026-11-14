import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["FORGE_DATA"] = tempfile.mkdtemp()

from server import generator  # noqa: E402


class TestSoftwareGL(unittest.TestCase):
    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"FORGE_SOFTWARE_GL": "1"}):
            self.assertTrue(generator.software_gl())
        with mock.patch.dict(os.environ, {"FORGE_SOFTWARE_GL": "0"}):
            self.assertFalse(generator.software_gl())

    def test_linux_without_dri_is_software(self):
        # this Windows dev box: /dev/dri never exists, but the auto rule is
        # linux-only, so detection must NOT flip on here
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FORGE_SOFTWARE_GL", None)
            if sys.platform.startswith("linux"):
                expected = not os.path.exists("/dev/dri")
                self.assertEqual(generator.software_gl(), expected)
            else:
                self.assertFalse(generator.software_gl())

    def test_render_env_software_recipe(self):
        with mock.patch.dict(os.environ, {"FORGE_SOFTWARE_GL": "1"}):
            env = generator._render_env(24)
            self.assertEqual(env["SOFTWARE_GL"], "1")
            self.assertEqual(env["QS"], "&res=0.6&aa=0&shadow=1024")
            self.assertEqual(env["FPS"], "24")
        with mock.patch.dict(os.environ, {"FORGE_SOFTWARE_GL": "0"}):
            env = generator._render_env(30)
            self.assertNotIn("SOFTWARE_GL", env)


class TestConcurrency(unittest.TestCase):
    def test_env_override(self):
        with mock.patch.dict(os.environ, {"FORGE_MAX_CONCURRENCY": "3"}):
            self.assertEqual(generator.max_concurrency(), 3)
        with mock.patch.dict(os.environ, {"FORGE_MAX_CONCURRENCY": "0"}):
            self.assertEqual(generator.max_concurrency(), 1)

    def test_small_cpu_default_is_1(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FORGE_MAX_CONCURRENCY", None)
            with mock.patch("os.cpu_count", return_value=2):
                self.assertEqual(generator.max_concurrency(), 1)

    def test_four_cpu_default_is_2(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FORGE_MAX_CONCURRENCY", None)
            with mock.patch("os.cpu_count", return_value=4):
                self.assertEqual(generator.max_concurrency(), 2)


class TestStatusProfileFields(unittest.TestCase):
    def test_status_exposes_profile(self):
        st = generator.status()
        self.assertIn("software_gl", st)
        self.assertIn("max_concurrency", st)
        self.assertIn("data_dir", st)


class TestChromeArgsLinux(unittest.TestCase):
    def test_args_present_on_linux(self):
        from server.accounts.manager import LAUNCH_ARGS
        self.assertIn("--disable-blink-features=AutomationControlled",
                      LAUNCH_ARGS)
        if sys.platform.startswith("linux"):
            self.assertIn("--disable-dev-shm-usage", LAUNCH_ARGS)


if __name__ == "__main__":
    unittest.main()
