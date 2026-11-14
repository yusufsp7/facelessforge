"""Round 6: stream key pada URL publish + AccountError handler."""
import json
import os
import socket
import sys
import tempfile
import time
import unittest
import threading

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
        c.get("/api/styles")
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


class TestR7Watchdog(unittest.TestCase):
    def _marker(self, until_offset):
        from server import paths, updater
        head = updater._git(["rev-parse", "HEAD"])[1].strip()
        marker = paths.data_root() / ".update-watchdog.json"
        marker.write_text(json.dumps({
            "prev_head": head, "new_head": head,
            "until": time.time() + until_offset}), encoding="utf-8")
        return head, marker

    @staticmethod
    def _dead_port():
        import socket as _s
        tmp = _s.socket(); tmp.bind(("127.0.0.1", 0)); port = tmp.getsockname()[1]; tmp.close()
        return port

    def test_healthy_server_no_rollback(self):
        """Server naik cepat → health OK → TIDAK rollback (probe sekali tembak = bug)."""
        import http.server
        from server import paths, updater
        updater._watchdog_started = False
        head, marker = self._marker(12)
        port = self._dead_port()
        os.environ["FORGE_PORT"] = str(port)

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers()
                self.wfile.write(b'{"ok":true}')
            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        updater.start_watchdog()
        time.sleep(6)
        srv.shutdown()
        events = [e["event"] for e in db.rows(
            "SELECT event FROM history ORDER BY id DESC LIMIT 10")]
        self.assertIn("update_health_ok", events)
        self.assertNotIn("update_rolled_back", events)
        del os.environ["FORGE_PORT"]

    def test_dead_server_rolls_back_at_deadline(self):
        """Server tak pernah naik → rollback TEPAT WAKTU di tenggat, BUKAN saat startup."""
        from server import paths, updater
        updater._watchdog_started = False
        head, marker = self._marker(5)
        os.environ["FORGE_PORT"] = str(self._dead_port())
        updater.start_watchdog()
        time.sleep(2)
        evs = [e["event"] for e in db.rows(
            "SELECT event FROM history ORDER BY id DESC LIMIT 5")]
        self.assertNotIn("update_rolled_back", evs)
        time.sleep(5)
        evs = [e["event"] for e in db.rows(
            "SELECT event FROM history ORDER BY id DESC LIMIT 5")]
        self.assertIn("update_rolled_back", evs)
        del os.environ["FORGE_PORT"]
