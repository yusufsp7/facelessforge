"""Regression tests for VM-feedback round 2 (Bugs 1-5, 7)."""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["FORGE_DATA"] = tempfile.mkdtemp()

from fastapi.testclient import TestClient  # noqa: E402

from server import db, generator  # noqa: E402


def _client():
    from server.main import app
    c = TestClient(app)
    c.get("/api/styles")  # non-exempt → middleware membuat token file
    tok = open(os.path.join(os.environ["FORGE_DATA"], ".api-token"),
               encoding="utf-8").read().strip()
    c.headers.update({"X-Forge-Token": tok})
    return c


class TestBug1Jobs(unittest.TestCase):
    """POST /api/jobs benar-benar membuat job + konversi WIB→UTC benar."""

    def setUp(self):
        db.init_db()
        db.upsert("accounts", {
            "key": "youtube:t1", "platform": "youtube", "label": "T",
            "profile_key": "yt_t1", "connected": 1,
            "channel_id": "UCtest123", "created_at": db.now_iso()})
        self.asset_id = "a-b1"
        db.upsert("assets", {
            "id": self.asset_id, "kind": "video", "title": "tes",
            "path": "", "engine": "faceless-shorts:inside-out",
            "meta_json": "{}", "qc_status": "approved",
            "created_at": db.now_iso()}, conflict_col="id")
        self.c = _client()

    def test_create_job_wib_to_utc(self):
        r = self.c.post("/api/jobs", json={
            "asset_id": self.asset_id,
            "account_keys": ["youtube:t1"],
            "mode": "at",
            "schedule_at": "2026-11-15T19:00:00",  # polos = WIB
            "visibility": "public",
            "caption": {}})
        self.assertEqual(r.status_code, 200, r.text)
        jid = r.json()["created"][0]
        job = db.one("SELECT * FROM jobs WHERE id = ?", (jid,))
        self.assertEqual(job["schedule_at"], "2026-11-15T12:00:00+00:00")
        lst = self.c.get("/api/jobs").json()
        row = next(x for x in lst if x["id"] == jid)
        self.assertEqual(row["schedule_tz"], "2026-11-15T19:00:00+07:00")
        self.assertEqual(row["channel_id"], "UCtest123")
        self.assertEqual(row["platform_post_id"] or "", "")

    def test_qc_gate_blocks_pending(self):
        db.insert("assets", {
            "id": "a-pending", "kind": "video", "title": "p",
            "path": "", "engine": "faceless-shorts:tall-tale",
            "meta_json": "{}", "qc_status": "pending",
            "created_at": db.now_iso()})
        r = self.c.post("/api/jobs", json={
            "asset_id": "a-pending", "account_keys": ["youtube:t1"],
            "mode": "now", "caption": {}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("QC", r.text)


class TestBug2Continue(unittest.TestCase):
    """POST /api/generations/{id}/continue ada + alur prepare→continue."""

    def setUp(self):
        db.init_db()
        self.c = _client()

    def test_continue_endpoint_exists_and_flows(self):
        r = self.c.post("/api/generate", json={
            "style": "inside-out", "topic": "tes continue",
            "mode": "prepare"})
        self.assertEqual(r.status_code, 200, r.text)
        gen_id = r.json()["gen_id"]
        # tunggu scaffold selesai (copy example — cepat)
        deadline = time.time() + 60
        status = ""
        while time.time() < deadline:
            g = self.c.get(f"/api/generations/{gen_id}").json()
            status = g["status"]
            if status in ("prepared", "failed"):
                break
            time.sleep(1)
        self.assertEqual(status, "prepared", g)
        self.assertTrue(os.path.isdir(g["project_dir"]))
        # continue → status bergerak keluar dari 'prepared'
        r2 = self.c.post(f"/api/generations/{gen_id}/continue")
        self.assertEqual(r2.status_code, 200, r2.text)
        time.sleep(2)
        g2 = self.c.get(f"/api/generations/{gen_id}").json()
        self.assertNotEqual(g2["status"], "prepared")
        self.assertIn(g2["status"], ("queued", "voice", "failed", "timing",
                                     "checking", "rendering", "scaffolding",
                                     "writing"))

    def test_continue_unknown_400(self):
        r = self.c.post("/api/generations/tidakada/continue")
        self.assertEqual(r.status_code, 400)


class TestBug3Duration(unittest.TestCase):
    """Durasi WAJIB dari cues.json (bukan estimasi) + QC toleransi."""

    def test_expected_duration_from_cues(self):
        proj = os.path.join(os.path.dirname(generator.__file__),
                            "..", "vendor", "faceless-shorts", "styles",
                            "inside-out", "example")
        d, src = generator._expected_duration(os.path.abspath(proj))
        self.assertEqual(src, "cues")
        self.assertAlmostEqual(d, 36.454, delta=0.01)

    def test_qc_tolerance_logic(self):
        import subprocess as sp
        p = os.path.join(os.environ["FORGE_DATA"], "qc-dur.mp4")
        try:
            sp.run(["ffmpeg", "-v", "error",
                    "-f", "lavfi", "-i",
                    "testsrc=size=1080x1920:rate=10:duration=26",
                    "-f", "lavfi", "-i", "sine=duration=6",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", "-y", p],
                   check=True, capture_output=True, timeout=120)
        except Exception:
            self.skipTest("ffmpeg tidak tersedia")
        # render 25.5s vs harapan 36.45s → di luar toleransi → qc gagal
        r = generator.qc_validate(p, expected_s=36.454, dur_source="cues")
        self.assertFalse(r["ok"])
        self.assertIn("duration_match", r["checks"])
        self.assertFalse(r["checks"]["duration_match"])
        self.assertIn("≠", r.get("error", ""))


class TestBug4QcSheet(unittest.TestCase):
    """qc_sheet disalin ke media persisten & tersaji via /api/preview-file."""

    def test_sheet_persisted_and_served(self):
        from fastapi.testclient import TestClient
        from server.main import app
        c = _client()
        asset_id = "a-sheet"
        fake_src = os.path.join(os.environ["FORGE_DATA"], "sheet-src.jpg")
        import shutil
        # buat file kecil apa pun
        open(fake_src, "wb").write(b"\xff\xd8fakejpgdata")
        dst = generator._persist_qc_sheet(fake_src, asset_id)
        self.assertTrue(os.path.isfile(dst))
        self.assertIn("qc-" + asset_id, dst)
        r = c.get(f"/api/preview-file?path={dst}")
        self.assertEqual(r.status_code, 200)
        # path di luar data/ ditolak
        r2 = c.get("/api/preview-file?path=/etc/hosts")
        self.assertEqual(r2.status_code, 403)


class TestBug5ResumeSweep(unittest.TestCase):
    """Generasi non-terminal diantrekan ulang saat startup."""

    def test_resume_accepts_intermediate(self):
        db.init_db()
        proj = tempfile.mkdtemp(dir=os.environ["FORGE_DATA"])
        db.insert("generations", {
            "id": "g-resume", "style": "inside-out", "topic": "x",
            "status": "rendering", "project_dir": proj, "mode": "auto",
            "chunks_done": 1, "chunks_total": 6,
            "created_at": db.now_iso(), "updated_at": db.now_iso()})
        # checkpoint: chunk-0 sudah ada → resume tidak me-render ulang
        os.makedirs(os.path.join(proj, "renders"), exist_ok=True)
        json.dump({"done": ["chunk-0000"], "total": 6, "fps": 24},
                  open(os.path.join(proj, "renders", "checkpoint.json"), "w"))
        n = generator.resume_active_generations()
        g = generator.get_gen("g-resume")
        self.assertEqual(g["status"], "queued")
        self.assertGreaterEqual(n, 1)
        # continue menerima status antara
        r = generator.continue_generation("g-resume")
        self.assertTrue(r.get("ok"), r)


class TestBug7NoDraft(unittest.TestCase):
    def test_final_render_has_no_draft(self):
        src = open(os.path.join(os.path.dirname(generator.__file__),
                                "generator.py"), encoding="utf-8").read()
        chunk_block = src.split("def _chunked_render")[1].split(
            "def _expected_duration")[0]
        self.assertNotIn("--draft", chunk_block)


if __name__ == "__main__":
    unittest.main()
