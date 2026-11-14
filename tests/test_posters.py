import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.posters import registry  # noqa: E402
from server.posters.base import BasePoster, UploadError  # noqa: E402
from server.posters.tiktok import (build_caption, tt_classify_dialog_label,  # noqa: E402
                                   tt_norm_label)


class TestRegistry(unittest.TestCase):
    def test_all_platforms(self):
        self.assertEqual(
            set(registry.keys()),
            {"tiktok", "youtube", "facebook", "instagram", "threads"})


class TestTikTokDialogClassifier(unittest.TestCase):
    def test_deny_first(self):
        # 'Allow & post now' contains deny 'now'? no — deny list is about
        # destructive words; now-phrase only skips. test real deny:
        self.assertEqual(tt_classify_dialog_label("Don't allow"), "deny")
        self.assertEqual(tt_classify_dialog_label("Discard changes"), "deny")
        self.assertEqual(tt_classify_dialog_label("Buang dan keluar"), "deny")

    def test_allow_schedule(self):
        self.assertEqual(tt_classify_dialog_label("Allow scheduling"),
                         "allow")
        self.assertEqual(tt_classify_dialog_label("Jadwalkan postingan"),
                         "allow")

    def test_ack(self):
        self.assertEqual(tt_classify_dialog_label("I understand"), "ack")
        self.assertEqual(tt_classify_dialog_label("Saya mengerti"), "ack")

    def test_neutral(self):
        self.assertEqual(tt_classify_dialog_label("Kanggo"), "neutral")


class TestCaption(unittest.TestCase):
    def test_build(self):
        cap = build_caption("Judul", "Desk", ["news", "#viral"])
        self.assertIn("Judul", cap)
        self.assertIn("Desk", cap)
        self.assertIn("#news", cap)
        self.assertIn("#viral", cap)


class TestPreflight(unittest.TestCase):
    def test_missing_file(self):
        class P(BasePoster):
            platform = "tiktok"

            def login_check_js(self):
                return "true"

        p = P("unit_test")
        with self.assertRaises(UploadError) as ctx:
            p._preflight("Z:/definitely/missing/file.mp4")
        self.assertEqual(ctx.exception.stage, "preflight")


if __name__ == "__main__":
    unittest.main()
