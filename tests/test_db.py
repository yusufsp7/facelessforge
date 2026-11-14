import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import os as _os
_tmp = tempfile.mkdtemp()
_os.environ["FORGE_DATA"] = _tmp

from server import db  # noqa: E402


class TestDb(unittest.TestCase):
    def test_init_and_crud(self):
        db.init_db()
        db.init_db()  # idempotent
        db.insert("accounts", {
            "key": "tiktok:default", "platform": "tiktok", "label": "Main",
            "profile_key": "tiktok_default", "connected": 1,
            "created_at": db.now_iso(),
        })
        row = db.one("SELECT * FROM accounts WHERE key = ?", ("tiktok:default",))
        self.assertIsNotNone(row)
        self.assertEqual(row["platform"], "tiktok")
        db.update("accounts", "tiktok:default", {"account_name": "aku"},
                  key_col="key")
        row = db.one("SELECT account_name FROM accounts WHERE key = ?",
                     ("tiktok:default",))
        self.assertEqual(row["account_name"], "aku")

    def test_unique_profile_key(self):
        db.init_db()
        db.insert("accounts", {
            "key": "yt:a", "platform": "youtube", "profile_key": "dup",
            "created_at": db.now_iso(),
        })
        with self.assertRaises(Exception):
            db.insert("accounts", {
                "key": "yt:b", "platform": "youtube", "profile_key": "dup",
                "created_at": db.now_iso(),
            })

    def test_history(self):
        db.init_db()
        db.log_event(None, "boot", {"v": 1})
        self.assertGreater(len(db.rows("SELECT * FROM history")), 0)


if __name__ == "__main__":
    unittest.main()
