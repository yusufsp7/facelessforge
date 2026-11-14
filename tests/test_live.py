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
        """R6: browser proxy = relay lokal 127.0.0.1 (kredensial dibaca
        relay per koneksi dari env, bukan diteruskan ke Playwright)."""
        with mock.patch.dict(os.environ, {"https_proxy":
                                          "http://u:p%40s@10.0.0.1:3128"}):
            p = proxy_options()
            self.assertEqual(p["server"], "http://127.0.0.1:"
                             + str(proxy_options()["server"]).split(":")[-1])
            self.assertNotIn("10.0.0.1", p["server"])
            self.assertNotIn("username", p)  # tanpa auth di browser side

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


class TestProgressRegexRealFfmpeg(unittest.TestCase):
    """R8-nit: regex harus cocok baris statistik ffmpeg ASLI — KiB (bukan
    kB) dan prefiks Lsize= pada baris terakhir."""

    def _run(self, lines):
        p = {"log_path": os.path.join(os.environ["FORGE_DATA"], "enc.log"),
             "bytes": 0, "last_progress_ts": 0}
        open(p["log_path"], "w", encoding="utf-8").write("\n".join(lines))
        live._read_encoder_progress("youtube:x", p)
        return p

    def test_real_kib_line(self):
        p = self._run([
            "size=    6982KiB time=00:00:41.05 bitrate=1391.7kbits/s speed=1.04x",
        ])
        self.assertEqual(p["bytes"], 6982 * 1024)
        self.assertGreater(p["last_progress_ts"], 0)

    def test_real_kib_line_grows(self):
        p = self._run([
            "size=    3012KiB time=00:00:18.10 bitrate=1363.1kbits/s speed=1.05x",
            "size=    6982KiB time=00:00:41.05 bitrate=1391.7kbits/s speed=1.04x",
        ])
        self.assertEqual(p["bytes"], 6982 * 1024)

    def test_final_lsize_line(self):
        p = self._run([
            "size=    7082KiB time=00:00:41.66 bitrate=1391.4kbits/s speed=1.04x",
            "[out#0/flv] video:6700kB audio:120kB subtitle:0kB other:0kB",
            "Lsize=    7082KiB time=00:00:41.66 bitrate=1391.4kbits/s speed=1.04x",
        ])
        self.assertEqual(p["bytes"], 7082 * 1024)

    def test_old_kib_style_still_accepted(self):
        p = self._run(["size=    1234kB time=00:00:05.00 bitrate=800.0kbits/s"])
        self.assertEqual(p["bytes"], 1234 * 1024)

    def test_no_progress_lines_no_bytes(self):
        p = self._run(["frame= 42 fps=24 q=-1.0 size=N/A time=00:00:01.75"])
        self.assertEqual(p["bytes"], 0)
