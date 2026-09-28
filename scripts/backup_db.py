"""Create a consistent SQLite backup without copying a live WAL database file."""
import argparse
from contextlib import closing
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def backup(source: Path, destination: Path):
    source, destination = source.resolve(), destination.resolve()
    if not source.is_file():
        raise ValueError("Source database does not exist")
    if source == destination:
        raise ValueError("Backup must not overwrite the source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation refuses to overwrite an earlier backup.
    with destination.open("xb"):
        pass
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(destination)) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Backup integrity check failed")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--source", type=Path, help="Defaults to configured DB_PATH")
    args = parser.parse_args()
    if args.source is None:
        from quran_bot.config import Config
        args.source = Config.load().db_path
    backup(args.source, args.destination)
    print(f"Backup created: {args.destination.resolve()}")


if __name__ == "__main__":
    main()
