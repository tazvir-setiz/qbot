"""SQLite persistence with additive migration of the original settings table."""
from contextlib import asynccontextmanager
from pathlib import Path
import json

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
    "completed": "INTEGER DEFAULT 0", "delivery_pending": "INTEGER DEFAULT 0",
    "ai_enabled": "INTEGER DEFAULT 0", "ai_base_url": "TEXT", "ai_api_key": "TEXT",
    "ai_model": "TEXT", "ai_system_prompt": "TEXT",
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
            await db.execute("CREATE TABLE IF NOT EXISTS catalog (id INTEGER PRIMARY KEY CHECK(id=1), content TEXT NOT NULL, revision INTEGER NOT NULL)")
            await db.execute("""CREATE TABLE IF NOT EXISTS ai_media (
                owner_id INTEGER NOT NULL,
                media_key TEXT NOT NULL,
                media_type TEXT NOT NULL,
                file_id TEXT NOT NULL,
                description TEXT,
                PRIMARY KEY(owner_id, media_key)
            )""")
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

    async def get_catalog(self):
        async with self.connection() as db:
            async with db.execute("SELECT content, revision FROM catalog WHERE id=1") as cursor:
                row = await cursor.fetchone()
                return (json.loads(row[0]), row[1]) if row else None

    async def replace_catalog(self, previous, updated, expected_revision):
        """Commit catalog and remapped progress together, or change neither."""
        from .catalog import normalize_name
        indices = {normalize_name(s['name']): i for i, s in enumerate(updated)}
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT revision FROM catalog WHERE id=1") as cursor:
                version = await cursor.fetchone()
            if (version[0] if version else 0) != expected_revision:
                raise ValueError("فهرست توسط مدیر دیگری تغییر کرده؛ فایل را دوباره ارسال کنید.")
            async with db.execute("SELECT * FROM settings") as cursor:
                rows = await cursor.fetchall()
            if any(row["delivery_pending"] for row in rows):
                raise ValueError("نتیجهٔ یک ارسال نامشخص است؛ ابتدا گروه را بررسی و صفحهٔ صحیح را انتخاب کنید.")
            for row in rows:
                old_index = row["current_index"]
                index, page, day = None, None, 1
                if old_index is not None and 0 <= old_index < len(previous):
                    index = indices.get(normalize_name(previous[old_index]['name']))
                    if index is not None:
                        surah = updated[index]
                        page = row["current_page"]
                        if page is not None and surah['start_page'] <= page <= surah['end_page']:
                            day = row['current_day']
                        else:
                            page = surah['start_page']
                await db.execute("UPDATE settings SET current_index=?, current_page=?, current_day=?, active=0, next_run=NULL, is_first_message=1, completed=0, last_error=NULL WHERE chat_id=?",
                                 (index, page, day, row['chat_id']))
            revision = expected_revision + 1
            await db.execute("INSERT INTO catalog(id,content,revision) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET content=excluded.content,revision=excluded.revision",
                             (json.dumps(updated, ensure_ascii=False), revision))
            await db.commit()
            return revision


    async def find_ai_owner(self, destination_id: int):
        async with self.connection() as db:
            async with db.execute("""SELECT * FROM settings
                WHERE ai_enabled=1 AND destination_id=?
                  AND COALESCE(ai_base_url, '')<>''
                  AND COALESCE(ai_api_key, '')<>''
                  AND COALESCE(ai_model, '')<>''
                ORDER BY chat_id LIMIT 1""", (destination_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def list_ai_media(self, owner_id: int):
        async with self.connection() as db:
            async with db.execute("SELECT * FROM ai_media WHERE owner_id=? ORDER BY media_key", (owner_id,)) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def save_ai_media(self, owner_id: int, media_key: str, media_type: str, file_id: str, description: str = ""):
        async with self.connection() as db:
            await db.execute("""INSERT INTO ai_media(owner_id,media_key,media_type,file_id,description)
                VALUES(?,?,?,?,?)
                ON CONFLICT(owner_id,media_key) DO UPDATE SET
                  media_type=excluded.media_type,file_id=excluded.file_id,description=excluded.description""",
                (owner_id, media_key, media_type, file_id, description))
            await db.commit()

    async def delete_ai_media(self, owner_id: int, media_key: str):
        async with self.connection() as db:
            await db.execute("DELETE FROM ai_media WHERE owner_id=? AND media_key=?", (owner_id, media_key))
            await db.commit()
