"""Preserve file order: existing database indices refer to this order."""
from pathlib import Path


def load_surahs(path: Path) -> list[dict]:
    return parse_surahs(path.read_text(encoding="utf-8-sig"))


def parse_surahs(text: str) -> list[dict]:
    result = []
    names = set()
    for number, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        try:
            name, start, end = raw.split("|")
            start, end = int(start), int(end)
            name = name.strip()
            key = normalize_name(name)
            if not name or len(name) > 80 or key in names or any(ord(c) < 32 for c in name) or not 1 <= start <= end <= 604:
                raise ValueError
        except ValueError:
            raise ValueError(f"سطر {number} نامعتبر است؛ نام یکتا و بازهٔ صفحه بین ۱ و ۶۰۴ لازم است.") from None
        names.add(key)
        result.append(dict(name=name, start_page=start, end_page=end))
    if not result:
        raise ValueError("فهرست سوره‌ها خالی است.")
    if len(result) > 114:
        raise ValueError("فهرست نباید بیش از ۱۱۴ سوره داشته باشد.")
    return result


def normalize_name(value: str) -> str:
    return "".join(value.translate(str.maketrans("يكأإآ", "یکااا")).split()).replace("\u200c", "")


def export_surahs(surahs):
    return "\n".join(f"{s['name']}|{s['start_page']}|{s['end_page']}" for s in surahs) + "\n"


def validate_replacement(surahs, previous):
    if {normalize_name(s['name']) for s in surahs} != {normalize_name(s['name']) for s in previous}:
        raise ValueError("فایل باید همهٔ سوره‌های فهرست فعلی را دقیقاً یک بار داشته باشد؛ از فایل الگو استفاده کنید.")
