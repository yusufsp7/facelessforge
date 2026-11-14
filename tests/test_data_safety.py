"""Round 8: update tidak boleh menimpa data akun."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["FORGE_DATA"] = tempfile.mkdtemp()

from server import db, updater  # noqa: E402

db.init_db()


class TestDataGuard(unittest.TestCase):
    def test_backup_preserves_accounts(self):
        db.upsert("accounts", {"key": "youtube:b1", "platform": "youtube",
                               "connected": 1, "created_at": db.now_iso()})
        bdir = updater._backup_data("test1")
        self.assertTrue(os.path.isfile(os.path.join(bdir, "forge.db")))
        # hapus akun → restore dari backup → kembali
        conn = db.connect()
        conn.execute("DELETE FROM accounts WHERE key = 'youtube:b1'")
        conn.commit()
        conn.close()
        self.assertEqual(updater._accounts_count(db.paths.db_path()), 0)
        n = updater._restore_accounts_from_backup(bdir)
        self.assertGreaterEqual(n, 1)
        row = db.one("SELECT * FROM accounts WHERE key = 'youtube:b1'")
        self.assertIsNotNone(row)
        self.assertEqual(row["platform"], "youtube")

    def test_data_paths_in_diff(self):
        # head kini tak menyentuh data/ → kosong
        self.assertEqual(updater._data_paths_in_diff("HEAD", "HEAD"), [])
        # diff fiktif: bandingkan HEAD dengan commit vendor (ada file di
        # vendor/ tapi bukan data/) → tetap kosong utk data/
        head = updater._git(["rev-parse", "HEAD"])[1].strip()
        first = updater._git(["rev-list", "--max-parents=0", "HEAD"])
        if first[1].strip():
            out = updater._data_paths_in_diff(first[1].strip(), head)
            self.assertTrue(all(not x.startswith("data/") for x in out))

    def test_backup_survives_simulated_reinstall(self):
        """Simulasi: db lama hilang (VM diganti) → restore dari backup."""
        db.upsert("accounts", {"key": "youtube:b2", "platform": "youtube",
                               "connected": 1, "created_at": db.now_iso()})
        db.upsert("live_profiles", {"account_key": "youtube:b2",
                                    "stream_key": "sk-rahasia",
                                    "created_at": db.now_iso()},
                  conflict_col="account_key")
        bdir = updater._backup_data("test2")
        conn = db.connect()
        conn.execute("DELETE FROM accounts WHERE key = 'youtube:b2'")
        conn.execute("DELETE FROM live_profiles WHERE account_key = 'youtube:b2'")
        conn.commit()
        conn.close()
        updater._restore_accounts_from_backup(bdir)
        self.assertIsNotNone(db.one(
            "SELECT * FROM accounts WHERE key = 'youtube:b2'"))
        lp = db.one("SELECT * FROM live_profiles WHERE account_key = 'youtube:b2'")
        self.assertEqual(lp["stream_key"], "sk-rahasia")


if __name__ == "__main__":
    unittest.main()
