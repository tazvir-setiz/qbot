from contextlib import closing
import importlib.util
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backup_tool = load_script("backup_tool", "scripts/backup_db.py")
worker = load_script("cpanel_worker", "deploy/cpanel_worker.py")


class BackupTests(unittest.TestCase):
    def test_backup_includes_committed_wal_data(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.db"
            target = Path(directory) / "backup.db"
            with closing(sqlite3.connect(source)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE sample (value TEXT)")
                db.execute("INSERT INTO sample VALUES ('saved')")
                db.commit()
                backup_tool.backup(source, target)
                with closing(sqlite3.connect(target)) as restored:
                    self.assertEqual(restored.execute("SELECT value FROM sample").fetchone()[0], "saved")

    def test_backup_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "source.db", Path(directory) / "backup.db"
            source.touch()
            target.write_bytes(b"keep this backup")
            with self.assertRaises(FileExistsError):
                backup_tool.backup(source, target)
            self.assertEqual(target.read_bytes(), b"keep this backup")

    def test_missing_source_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "missing.db", Path(directory) / "backup.db"
            with self.assertRaises(ValueError):
                backup_tool.backup(source, target)
            self.assertFalse(target.exists())


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lock_api = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=Mock())
        self.patches = [patch.object(worker, "ROOT", self.root),
                        patch.dict("sys.modules", {"fcntl": self.lock_api}),
                        patch.object(worker.os, "umask")]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        level = logging.getLogger().level
        self.addCleanup(logging.getLogger().setLevel, level)

    def test_disabled_worker_does_not_start(self):
        (self.root / "run").mkdir()
        (self.root / "run" / "disabled").touch()
        with patch("quran_bot.application.main") as main:
            self.assertEqual(worker.run(), 0)
            main.assert_not_called()

    def test_duplicate_worker_exits_without_starting(self):
        self.lock_api.flock.side_effect = BlockingIOError
        with patch("quran_bot.application.main") as main:
            self.assertEqual(worker.run(), 0)
            main.assert_not_called()
        self.assertFalse((self.root / "run" / "worker.pid").exists())

    def test_pid_and_rotating_logs_exist_only_for_running_worker(self):
        def fake_main():
            self.assertTrue((self.root / "run" / "worker.pid").exists())
            logging.getLogger("worker-test").info("test worker started")
        with patch("quran_bot.config.Config.load", return_value=SimpleNamespace(log_level="INFO")), patch("quran_bot.application.main", side_effect=fake_main):
            self.assertEqual(worker.run(), 0)
        self.assertFalse((self.root / "run" / "worker.pid").exists())
        self.assertIn("test worker started", (self.root / "logs" / "bot.log").read_text(encoding="utf-8"))

    def test_worker_failure_cleans_pid(self):
        with patch("quran_bot.config.Config.load", return_value=SimpleNamespace(log_level="INFO")), patch("quran_bot.application.main", side_effect=RuntimeError("failure")):
            self.assertEqual(worker.run(), 1)
        self.assertFalse((self.root / "run" / "worker.pid").exists())


class DeploymentConfigTests(unittest.TestCase):
    def test_railway_uses_existing_worker_entrypoint(self):
        config = json.loads((ROOT / "railway.json").read_text(encoding="utf-8"))
        self.assertTrue((ROOT / config["build"]["dockerfilePath"]).is_file())
        self.assertEqual(config["deploy"]["startCommand"], "python -u main.py")
        self.assertTrue((ROOT / "main.py").is_file())
        self.assertNotIn("healthcheckPath", config["deploy"])


if __name__ == "__main__":
    unittest.main()
