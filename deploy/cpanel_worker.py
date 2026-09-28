"""Linux/cPanel cron entry point. The host must permit long-lived workers."""
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def run():
    try:
        import fcntl
    except ImportError:
        raise SystemExit("This launcher requires Linux/POSIX; use main.py on Windows.") from None

    os.umask(0o077)
    runtime = ROOT / "run"
    runtime.mkdir(exist_ok=True)
    if (runtime / "disabled").exists():
        return 0
    # Keep this inode in place. Deleting the lock file while running defeats flock.
    with (runtime / "worker.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        if (runtime / "disabled").exists():
            return 0
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        handler = RotatingFileHandler(logs / "bot.log", maxBytes=5 * 1024 * 1024,
                                      backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        pid_file = runtime / "worker.pid"
        pid_file.write_text(str(os.getpid()), encoding="ascii")
        try:
            from quran_bot.application import main
            from quran_bot.config import Config
            root_logger.setLevel(Config.load().log_level)
            main()
            return 0
        except Exception as exc:
            # Startup errors go to the rotating log, without dumping credentials.
            root_logger.error("Worker exited with %s. Check configuration and run main.py interactively for diagnostics.", type(exc).__name__)
            return 1
        finally:
            pid_file.unlink(missing_ok=True)
            root_logger.removeHandler(handler)
            handler.close()


if __name__ == "__main__":
    raise SystemExit(run())
