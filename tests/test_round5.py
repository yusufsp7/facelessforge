"""Round 5/6 regression: proxy relay, partial live PUT, tunnel pipe, jobs."""
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["FORGE_DATA"] = tempfile.mkdtemp()

from server import db, generator, live  # noqa: E402
from server.browser import proxy_options  # noqa: E402

db.init_db()


class TestR2ChainRelay(unittest.TestCase):
    def test_browser_proxy_uses_local_relay(self):
        os.environ["https_proxy"] = "http://198.19.0.1:3128"
        p = proxy_options()
        self.assertEqual(p["server"].startswith("http://127.0.0.1:"), True)
        self.assertNotIn("198.19.0.1", p["server"])  # relay menengok env sendiri

    def test_relay_pipe_end_to_end(self):
        """Tunnel pipe mekanik: client → relay → CONNECT → upstream target.
        (setara ffmpeg → tunnel → ingest)"""
        tgt = socket.socket(); tgt.bind(("127.0.0.1", 0)); tgt.listen(1)
        tport = tgt.getsockname()[1]
        def fake_proxy():
            s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1)
            fake_proxy.port = s.getsockname()[1]
            c, _ = s.accept()
            req = b""
            while b"\r\n\r\n" not in req:
                req += c.recv(4096)
            self.assertIn(b"CONNECT 127.0.0.1:" + str(tport).encode(), req)
            c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            d = c.recv(1024); c.sendall(b"PONG:" + d); c.close()
        threading.Thread(target=fake_proxy, daemon=True).start()
        for _ in range(50):
            if getattr(fake_proxy, "port", None):
                break
            time.sleep(0.05)
        os.environ["https_proxy"] = f"http://127.0.0.1:{fake_proxy.port}"
        relay = proxy_options()["server"]
        cli = socket.create_connection(
            ("127.0.0.1", int(relay.split(":")[-1])), timeout=5)
        cli.sendall(f"CONNECT 127.0.0.1:{tport} HTTP/1.1\r\n"
                    "Host: x\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += cli.recv(4096)
        self.assertIn(b" 200 ", buf.split(b"\r\n")[0])
        cli.sendall(b"PING")
        self.assertEqual(cli.recv(100), b"PONG:PING")


class TestBug3PartialLivePut(unittest.TestCase):
    def test_partial_body_does_not_crash(self):
        db.upsert("accounts", {"key": "youtube:lp", "platform": "youtube",
                               "connected": 1, "created_at": db.now_iso()})
        live.save_profile("youtube:lp", {"stream_key": "sk-1234567890",
                                         "media_path": "/v/m.mp4",
                                         "loop": 1, "desired_on": 1})
        # body parsial ala model_dump() (None untuk yang tidak dikirim)
        p = live.save_profile("youtube:lp", {
            "stream_key": "••••7890", "ingest_url": None,
            "ingest_backup": None, "media_path": None, "loop": None,
            "desired_on": None, "broadcast_id": None})
        self.assertEqual(p["stream_key"], "sk-1234567890")
        self.assertEqual(p["media_path"], "/v/m.mp4")
        self.assertEqual(p["loop"], 1)
        self.assertEqual(p["desired_on"], 1)


class TestBug1UpdaterApply(unittest.TestCase):
    def test_apply_exists_and_gates(self):
        from server import updater
        self.assertTrue(callable(updater.apply))
        # repo ada → ok/restarting; bukan "no attribute"
        r = updater.apply()
        self.assertIn(r.get("ok"), (True, False))
        updater._updating = False  # reset state untuk test lain


if __name__ == "__main__":
    unittest.main()
