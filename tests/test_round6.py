"""Round 6: stream key pada URL publish + AccountError handler."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["FORGE_DATA"] = tempfile.mkdtemp()

from server import db, live  # noqa: E402

db.init_db()


class TestDestUrl(unittest.TestCase):
    PROFILE = {"stream_key": "abcd-12xy-wxyz-34ef",
               "ingest_url": "rtmp://a.rtmp.youtube.com/live2"}

    def test_plain_includes_key(self):
        url = live._dest_url(self.PROFILE, None)
        self.assertEqual(url,
                         "rtmp://a.rtmp.youtube.com/live2/abcd-12xy-wxyz-34ef")

    def test_tunnel_includes_key(self):
        url = live._dest_url(self.PROFILE, 45678)
        self.assertEqual(url,
                         "rtmp://127.0.0.1:45678/live2/abcd-12xy-wxyz-34ef")

    def test_ffmpeg_cmd_gets_keyed_url(self):
        p = {**self.PROFILE, "media_path": "/v/m.mp4", "loop": 1}
        cmd = live._ffmpeg_cmd(p, live._dest_url(p, None))
        self.assertIn("abcd-12xy-wxyz-34ef", cmd[-1])

    def test_no_key_falls_back_clean(self):
        p = {**self.PROFILE, "stream_key": ""}
        url = live._dest_url(p, None)
        self.assertEqual(url, "rtmp://a.rtmp.youtube.com/live2")


class TestAccountErrorHandler(unittest.TestCase):
    def test_account_error_becomes_400(self):
        from fastapi.testclient import TestClient
        from server.accounts.manager import MANAGER
        from server.accounts.manager import AccountError
        from server.main import app
        c = TestClient(app)
        c.get("/api/styles")  # non-exempt → token dibuat
        tok = open(os.path.join(os.environ["FORGE_DATA"], ".api-token"),
                   encoding="utf-8").read().strip()
        c.headers.update({"X-Forge-Token": tok})

        # endpoint tanpa try/except lokal → handler global yang menangkap
        orig = MANAGER.import_cookies
        MANAGER.import_cookies = lambda key, cookies: (_ for _ in ()).throw(
            AccountError("cookies diterima tapi verifikasi gagal: boom"))
        try:
            r = c.post("/api/accounts/youtube:x/cookies", json=[{}])
            self.assertEqual(r.status_code, 400)
            self.assertIn("boom", r.json()["detail"])
        finally:
            MANAGER.import_cookies = orig


if __name__ == "__main__":
    unittest.main()
