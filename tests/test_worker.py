import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["FORGE_DATA"] = tempfile.mkdtemp()

from server import db  # noqa: E402
from server.posters.base import UploadResult  # noqa: E402
from server.worker import Worker  # noqa: E402


class FakePoster:
    calls = []

    def __init__(self, profile_key):
        self.profile_key = profile_key

    def upload(self, *, asset_path, asset_kind, title, description, tags,
               schedule_at, visibility, progress_cb):
        FakePoster.calls.append((self.profile_key, title))
        progress_cb(50)
        return UploadResult(ok=True, public_url="https://fake.test/x")


class TestWorkerFlow(unittest.TestCase):
    def test_full_flow(self):
        db.init_db()
        db.insert("accounts", {
            "key": "tiktok:test", "platform": "tiktok", "label": "T",
            "profile_key": "tiktok_test", "connected": 1,
            "created_at": db.now_iso()})
        db.insert("assets", {
            "id": "a1", "kind": "video", "title": "clip",
            "path": "", "engine": "fake",
            "meta_json": "{}", "created_at": db.now_iso()})
        db.insert("jobs", {
            "id": "j1", "asset_id": "a1", "account_key": "tiktok:test",
            "status": "queued", "schedule_at": db.now_iso(),
            "visibility": "public",
            "caption_json": '{"title":"halo","schedule_native":false}',
            "created_at": db.now_iso(), "updated_at": db.now_iso()})

        w = Worker()
        w._poster_for = lambda platform: FakePoster  # monkeypatch registry
        w._tick()
        # wait for the job thread to finish
        for _ in range(60):
            job = db.one("SELECT * FROM jobs WHERE id = 'j1'")
            if job["status"] == "done":
                break
            time.sleep(0.2)
        job = db.one("SELECT * FROM jobs WHERE id = 'j1'")
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["public_url"], "https://fake.test/x")
        self.assertEqual(FakePoster.calls[0][1], "halo")
        events = db.rows("SELECT event FROM history WHERE job_id = 'j1'")
        self.assertIn("job_started", [e["event"] for e in events])
        self.assertIn("job_finished", [e["event"] for e in events])

    def test_missing_account_fails_job(self):
        db.init_db()
        db.insert("jobs", {
            "id": "j2", "asset_id": "a1", "account_key": "nope",
            "status": "queued", "schedule_at": db.now_iso(),
            "created_at": db.now_iso(), "updated_at": db.now_iso()})
        w = Worker()
        w._tick()
        job = db.one("SELECT * FROM jobs WHERE id = 'j2'")
        self.assertEqual(job["status"], "failed")


if __name__ == "__main__":
    unittest.main()
