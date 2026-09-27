"""SQLite persistence with additive migration of the original settings table."""
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

FIELDS = {
    "password_ok": "INTEGER DEFAULT 0", "current_index": "INTEGER", "current_page": "INTEGER",
    "current_day": "INTEGER DEFAULT 1", "interval_days": "INTEGER DEFAULT 0",
    "interval_hours": "INTEGER DEFAULT 0", "interval_minutes": "INTEGER DEFAULT 0",
    "interval_seconds": "INTEGER DEFAULT 0", "hour": "INTEGER", "minute": "INTEGER",
    "is_first_message": "INTEGER DEFAULT 1", "active": "INTEGER DEFAULT 0",
    "next_run": "REAL",
    "destination_id": "INTEGER", "destination_title": "TEXT",
    "last_error": "TEXT", "last_sent_at": "REAL",
}


class Repository:
    def __init__(self, path: Path):
        self.path = path

    @asynccontextmanager
    async def connection(self):
        async with aiosqlite.connect(self.path, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            yield db

    async def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.connection() as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("CREATE TABLE IF NOT EXISTS settings (chat_id INTEGER PRIMARY KEY)")
            async with db.execute("PRAGMA table_info(settings)") as cursor:
                columns = {row["name"] for row in await cursor.fetchall()}
            for name, definition in FIELDS.items():
                if name not in columns:
                    await db.execute(f"ALTER TABLE settings ADD COLUMN {name} {definition}")
            await db.execute("UPDATE settings SET current_day=1 WHERE current_day IS NULL OR current_day<1")
            await db.commit()

    async def ensure(self, chat_id: int):
        async with self.connection() as db:
            await db.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES (?)", (chat_id,))
            await db.commit()

    async def get(self, chat_id: int):
        async with self.connection() as db:
            async with db.execute("SELECT * FROM settings WHERE chat_id=?", (chat_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def update(self, chat_id: int, **values):
        if not values or not values.keys() <= FIELDS.keys():
            raise ValueError("Invalid settings fields")
        async with self.connection() as db:
            await db.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES (?)", (chat_id,))
            assignments = ", ".join(f"{name}=?" for name in values)
            await db.execute(f"UPDATE settings SET {assignments} WHERE chat_id=?", (*values.values(), chat_id))
            await db.commit()

    async def active_ids(self):
        async with self.connection() as db:
            async with db.execute("SELECT chat_id FROM settings WHERE active=1") as cursor:
                return [row[0] for row in await cursor.fetchall()]
