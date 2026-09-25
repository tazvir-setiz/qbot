"""Preserve file order: existing database indices refer to this order."""
from pathlib import Path


def load_surahs(path: Path) -> list[dict]:
    result = []
    names = set()
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        try:
            name, start, end = raw.split("|")
            start, end = int(start), int(end)
            name = name.strip()
            if not name or name in names or not 1 <= start <= end <= 604:
                raise ValueError
        except ValueError:
            raise ValueError(f"Invalid surah data on line {number}") from None
        names.add(name)
        result.append(dict(name=name, start_page=start, end_page=end))
    if not result:
        raise ValueError("Surah list is empty")
    return result


def normalize_name(value: str) -> str:
    return "".join(value.translate(str.maketrans("يكأإآ", "یکااا")).split()).replace("\u200c", "")
