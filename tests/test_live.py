"""Livestream module tests: masking, cmd builder, tunnel targets, proxy."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["FORGE_DATA"] = tempfile.mkdtemp()

from server import db, live  # noqa: E402
from server.browser import ignore_cert_errors, proxy_options  # noqa: E402

db.init_db()


class TestProfile(unittest.TestCase):
    def test_save_and_mask(self):
        db.upsert("accounts", {"key": "youtube:lv", "platform": "youtube",
                               "connected": 1, "created_at": db.now_iso()})
        p = live.save_profile("youtube:lv", {"stream_key": "abcd-1234-xyz9",
                                             "media_path": "x.mp4",
                                             "loop": 1})
        print("DEBUG save-result:", p["stream_key"])
        self.assertEqual(p["stream_key"], "abcd-1234-xyz9")  # internal
        pub = live.public_profile("youtube:lv")
        self.assertEqual(pub["stream_key_masked"], "••••xyz9")
        self.assertNotIn("abcd-1234-xyz9", str(pub))  # full key NEVER in API
        self.assertTrue(pub["has_stream_key"])
        print("DEBUG after-save:", live.get_profile("youtube:lv") is not None,
              db.rows("SELECT account_key FROM live_profiles"))

    def test_masked_update_keeps_secret(self):
        # UI mengirim "••••" → tetap pakai key lama, tidak menimpa.
        # Mandiri: buat profil dulu (urutan test tidak boleh dipakai).
        live.save_profile("youtube:lv", {"stream_key": "abcd-1234-xyz9"})
        live.save_profile("youtube:lv", {"stream_key": "••••xyz9"})
        p = live.get_profile("youtube:lv")
        self.assertEqual(p["stream_key"], "abcd-1234-xyz9")


class TestFfmpegCmd(unittest.TestCase):
    def test_cmd_loop_and_dest(self):
        p = {"media_path": "/v/pluto.mp4", "loop": 1,
             "ingest_url": "rtmp://a.rtmp.youtube.com/live2"}
        cmd = live._ffmpeg_cmd(p, "rtmp://x/live2/key")
        self.assertIn("-stream_loop", cmd)
        self.assertIn("-1", cmd)
        self.assertEqual(cmd[-1], "rtmp://x/live2/key")
        self.assertIn("flv", cmd)
        p2 = {**p, "loop": 0}
        self.assertNotIn("-stream_loop", live._ffmpeg_cmd(p2, "rtmp://x/k"))


class TestTunnel(unittest.TestCase):
    def test_target_parse(self):
        self.assertEqual(live._rtmp_proxy_target(
            "rtmp://a.rtmp.youtube.com/live2"),
            ("a.rtmp.youtube.com", 1935))
        self.assertEqual(live._rtmp_proxy_target(
            "rtmp://b.rtmp.youtube.com:1936/live2?backup=1"),
            ("b.rtmp.youtube.com", 1936))


class TestBrowserProxy(unittest.TestCase):
    def test_proxy_parse_with_credentials(self):
        with mock.patch.dict(os.environ, {"https_proxy":
                                          "http://u:p%40s@10.0.0.1:3128"}):
            p = proxy_options()
            self.assertEqual(p["server"], "http://10.0.0.1:3128")
            self.assertEqual(p["username"], "u")
            self.assertEqual(p["password"], "p@s")

    def test_no_proxy_no_options(self):
        env = {k: "" for k in ("https_proxy", "HTTPS_PROXY", "http_proxy",
                               "HTTP_PROXY", "all_proxy", "ALL_PROXY")}
        with mock.patch.dict(os.environ, env):
            self.assertIsNone(proxy_options())

    def test_ignore_cert_flag(self):
        with mock.patch.dict(os.environ, {"FORGE_IGNORE_CERT_ERRORS": "1"}):
            self.assertTrue(ignore_cert_errors())
        with mock.patch.dict(os.environ, {"FORGE_IGNORE_CERT_ERRORS": "0"}):
            self.assertFalse(ignore_cert_errors())


class TestDesiredOnPersistence(unittest.TestCase):
    def test_desired_on_survives(self):
        live.save_profile("youtube:lv", {"desired_on": 1})
        self.assertTrue(live.get_profile("youtube:lv")["desired_on"])
        live.stop("youtube:lv", clear_desired=True)
        self.assertFalse(live.get_profile("youtube:lv")["desired_on"])


if __name__ == "__main__":
    unittest.main()
