import jdatetime
import pytz

def to_persian_number(number: int) -> str:
    return str(number).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))

def get_persian_weekday(weekday_en: str) -> str:
    mapping = {
        "Saturday": "شنبه",
        "Sunday": "یکشنبه",
        "Monday": "دوشنبه",
        "Tuesday": "سه‌شنبه",
        "Wednesday": "چهارشنبه",
        "Thursday": "پنجشنبه",
        "Friday": "جمعه"
    }
    return mapping.get(weekday_en, weekday_en)

def jalali_now(timezone: str):
    tz = pytz.timezone(timezone)
    now = jdatetime.datetime.now(tz)
    weekday = get_persian_weekday(now.strftime("%A"))
    date_s = f"{to_persian_number(now.year)}/{to_persian_number(now.month)}/{to_persian_number(now.day)}"
    return weekday, date_s

def build_daily_message(sura, page: int, day: int, timezone: str):
    day_count = to_persian_number(day)
    start_page = to_persian_number(sura["start_page"])
    end_page = to_persian_number(sura["end_page"])
    weekday, date_s = jalali_now(timezone)
    msg = f"• روز {day_count}\nتدبری بر سورهٔ: {sura['name']} (از صفحه {start_page} تا صفحه {end_page})\nصفحهٔ: {to_persian_number(page)}\n\n- {weekday}، {date_s}"
    return msg


