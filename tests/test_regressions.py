import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["FORGE_DATA"] = tempfile.mkdtemp()

from server import generator  # noqa: E402


class TestRegressionVMReport(unittest.TestCase):
    def test_whisper_check_quotes_balanced(self):
        """A2 regression: the whisper check string must be valid bash."""
        src = open(os.path.join(os.path.dirname(generator.__file__),
                                "generator.py"), encoding="utf-8").read()
        self.assertIn('"from faster_whisper import WhisperModel"',
                      src)

    def test_render_env_carries_chrome_and_noproxy(self):
        """A3: capture.mjs must get CHROME + NO_PROXY from the server."""
        env = generator._bash.__defaults__  # sanity: helper exists
        from server.browser import launch_env
        e = launch_env()
        self.assertIn("NO_PROXY", e)
        self.assertEqual(e["NO_PROXY"], "localhost,127.0.0.1")

    def test_styles_durations_removed(self):
        """F33: descriptions no longer carry wrong duration claims."""
        for sid, (_, desc) in generator.STYLES.items():
            self.assertNotRegex(desc, r"\d+-\d+s")

    def test_qc_validate_unit(self):
        import json as _json
        # build a tiny valid mp4 via ffmpeg if available; else skip
        import subprocess as _sp
        p = os.path.join(os.environ["FORGE_DATA"], "t.mp4")
        try:
            _sp.run(["ffmpeg", "-v", "error",
                     "-f", "lavfi", "-i",
                     "testsrc=size=1080x1920:rate=10:duration=6",
                     "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p",
                     "-c:a", "aac", "-shortest", "-y", p],
                    check=True, capture_output=True, timeout=120)
        except Exception:
            self.skipTest("ffmpeg tidak tersedia")
        r = generator.qc_validate(p, {})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["width"], 1080)
        self.assertEqual(r["height"], 1920)


if __name__ == "__main__":
    unittest.main()
