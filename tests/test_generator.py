import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["FORGE_DATA"] = tempfile.mkdtemp()

from fastapi.testclient import TestClient  # noqa: E402

from server import generator  # noqa: E402


class TestStyleCatalog(unittest.TestCase):
    def test_six_styles(self):
        st = generator.list_styles()
        self.assertEqual(len(st), 6)
        ids = {s["id"] for s in st}
        self.assertEqual(ids, {"deep-dive", "night-file", "inside-out",
                               "stick-story", "paper-atlas", "tall-tale"})
        for s in st:
            self.assertTrue(s["label"])
            self.assertTrue(s["description"])
            self.assertTrue(s["sheet"].startswith("/api/styles/"))

    def test_sheet_files_exist(self):
        for sid in generator.STYLES:
            p = generator.style_sheet_path(sid)
            self.assertIsNotNone(p)
            self.assertGreater(os.path.getsize(p), 50_000)

    def test_unknown_style_rejected(self):
        with self.assertRaises(ValueError):
            generator.generate({"style": "tidak-ada", "topic": "x"})
        self.assertEqual(generator.style_sheet_path("tidak-ada"), None)


class TestStatus(unittest.TestCase):
    def test_status_shape(self):
        st = generator.status()
        for key in ("skill", "node", "ffmpeg", "chrome", "python_env",
                    "voice", "whisper", "ready", "hint"):
            self.assertIn(key, st)
        self.assertTrue(st["skill"])          # vendor installed
        self.assertTrue(st["node"])           # dev machine has node 24
        self.assertTrue(st["ffmpeg"])
        self.assertTrue(st["chrome"])

    def test_setup_helpers(self):
        self.assertEqual(generator.setup_log_tail(), [])
        r = generator.run_setup.__doc__  # just exists


class TestAPI(unittest.TestCase):
    def setUp(self):
        from server.main import app
        self.c = TestClient(app)
        # middleware generates the token on first blocked request, then
        # every request carries it (client host "testclient" ≠ localhost)
        self.c.get("/api/styles")
        tok = open(os.path.join(os.environ["FORGE_DATA"], ".api-token"),
                   encoding="utf-8").read().strip()
        self.c.headers.update({"X-Forge-Token": tok})

    def test_styles_endpoint(self):
        r = self.c.get("/api/styles")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()), 6)

    def test_sheet_endpoint(self):
        r = self.c.get("/api/styles/night-file/sheet.png")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/png")

    def test_generator_status_endpoint(self):
        r = self.c.get("/api/generator/status")
        self.assertEqual(r.status_code, 200)
        self.assertIn("ready", r.json())

    def test_generate_unknown_style_400(self):
        r = self.c.post("/api/generate", json={"style": "nope",
                                               "topic": "t"})
        self.assertEqual(r.status_code, 400)

    def test_generations_endpoint_empty(self):
        r = self.c.get("/api/generations")
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.json(), list)


if __name__ == "__main__":
    unittest.main()
