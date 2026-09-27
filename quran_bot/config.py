"""Configuration loaded relative to the project, regardless of working directory."""
import os
from dataclasses import dataclass
from pathlib import Path

import pytz

ROOT = Path(__file__).resolve().parent.parent


def read_env(path: Path) -> dict[str, str]:
    values = {}
    if path.exists():
        for line_number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or not key.strip().isidentifier():
                raise ValueError(f"Invalid .env entry at line {line_number}")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


@dataclass(frozen=True)
class Config:
    token: str
    group_id: int | None
    password: str
    timezone: str
    db_path: Path
    surah_path: Path
    admin_ids: frozenset[int]
    log_level: str = "INFO"

    @classmethod
    def load(cls, root: Path = ROOT):
        values = {**read_env(root / ".env"), **os.environ}
        for key in ("BOT_TOKEN", "ADMIN_PASSWORD"):
            if not values.get(key):
                raise ValueError(f"Missing required setting: {key}")
        token = values["BOT_TOKEN"]
        if ":" not in token or not token.split(":", 1)[0].isdigit():
            raise ValueError("Invalid BOT_TOKEN format")
        timezone = values.get("TIMEZONE", "Asia/Tehran")
        pytz.timezone(timezone)
        level = values.get("LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Invalid LOG_LEVEL")
        group_id = int(values["GROUP_ID"]) if values.get("GROUP_ID") else None
        if group_id is not None and group_id >= 0:
            raise ValueError("GROUP_ID must be a negative Telegram group ID")
        return cls(token, group_id, values["ADMIN_PASSWORD"], timezone,
                   (root / values.get("DB_PATH", "bot_data.db")).resolve(),
                   (root / values.get("SURAH_LIST_FILE", "surah_list.txt")).resolve(),
                   frozenset(int(v.strip()) for v in values.get("ADMIN_USER_IDS", "").split(",") if v.strip()), level)
