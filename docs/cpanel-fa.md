# راهنمای فارسی راه‌اندازی ربات روی cPanel

مبنای راهنما: همین پروژه با ورودی `main.py`، Python 3.11 یا جدیدتر، SQLite و polling. بررسی منابع: ۲۷ سپتامبر ۲۰۲۶. فرمان‌های Bash در Terminal/SSH سرور لینوکسی اجرا می‌شوند.

## ۱. تأیید قابلیت‌های هاست

cPanel فقط پنل مدیریت است و اجرای دائمی ربات را تضمین نمی‌کند. از پشتیبانی بپرسید:

> آیا پردازش دائمی Python 3.11+ برای Telegram long polling، Terminal/SSH، virtualenv، Cron هر دقیقه، قفل flock و ارتباط خروجی HTTPS با api.telegram.org مجاز هستند؟ آیا پردازش پس از خروج از SSH یا گذشت زمان مشخص کشته می‌شود؟

اگر فقط Passenger/WSGI یا Cron کوتاه مجاز است، نسخهٔ فعلی روی آن پلن مناسب نیست؛ Railway یا VPS/هاست دارای worker لازم است. نوشتن `main()` داخل `passenger_wsgi.py` درست نیست؛ این برنامه وب‌اپ WSGI نیست. Setup Python App به‌تنهایی جای اجرای پردازش دائمی را نمی‌گیرد. [Application Manager](https://docs.cpanel.net/cpanel/software/application-manager/)، [روش‌های Python در cPanel](https://support.cpanel.net/hc/en-us/articles/360049921014-How-do-I-set-up-a-Python-web-application)

فعال‌بودن Terminal به مجوز shell و سیاست میزبان وابسته است. [مستندات Terminal](https://docs.cpanel.net/cpanel/advanced/terminal-in-cpanel/)

## ۲. پوشه و آپلود

در تمام مثال‌ها `CPANEL_USER` را با نام کاربری واقعی عوض کنید. اگر Home Directory متفاوت است، همهٔ مسیرها را تغییر دهید:

```text
/home/CPANEL_USER/quran-bot
```

پوشه باید خارج از `public_html` و document root تمام دامنه‌ها باشد تا رمز و دیتابیس از وب قابل دریافت نباشند. با File Manager فایل‌ها را Upload/Extract کنید یا از Git Version Control استفاده کنید. فایل main باید مستقیماً در ریشهٔ پروژه باشد:

```text
quran-bot/
  main.py
  requirements.txt
  surah_list.txt
  .env.example
  quran_bot/
  deploy/cpanel_worker.py
  scripts/backup_db.py
  tests/
```

`.venv` ویندوز و فایل‌های IDE را منتقل نکنید. برای نصب تازه دیتابیس محلی لازم نیست؛ برای حفظ اطلاعات مرحلهٔ ۱۰ را ببینید.

## ۳. ساخت محیط Python

```bash
cd /home/CPANEL_USER/quran-bot
python3 --version
command -v python3.12
```

اگر `python3.12` موجود است:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

اگر فقط `python3` دارید و نسخهٔ آن حداقل 3.11 است، فرمان اول را با `python3 -m venv .venv` جایگزین کنید؛ محیط را دوبار نسازید. نبود Python مناسب یا venv را با میزبان حل کنید؛ حساب اشتراکی معمولاً اجازهٔ نصب سیستمی ندارد.

اگر میزبان محیط را فقط از Setup Python App می‌دهد، مسیر Python آن محیط را از پنل بگیرید و در تمام فرمان‌ها جای `.venv/bin/python` بگذارید. اجرای worker همچنان به مجوز میزبان نیاز دارد؛ حلقهٔ بات را به WSGI متصل نکنید.

## ۴. تنظیم `.env`

نمایش فایل‌های مخفی را در File Manager فعال کنید. برای نصب تازه:

```bash
cd /home/CPANEL_USER/quran-bot
cp -n .env.example .env
chmod 600 .env
mkdir -p run logs backups
chmod 700 run logs backups
```

`cp -n` تنظیمات موجود را بازنویسی نمی‌کند. با ویرایشگر File Manager پر کنید:

```dotenv
BOT_TOKEN=توکن_واقعی_بات
ADMIN_PASSWORD=رمز_واقعی_مدیریت
GROUP_ID=
TIMEZONE=Asia/Tehran
DB_PATH=bot_data.db
SURAH_LIST_FILE=surah_list.txt
ADMIN_USER_IDS=
LOG_LEVEL=INFO
```

متن فارسی نمونه را با مقدار واقعی عوض کنید. `ADMIN_USER_IDS` اختیاری است و شناسهٔ عددی مدیران را با کاما می‌گیرد، نه username. مقصد را بعداً با `/group` انتخاب کنید.

برنامه خودش `.env` را می‌خواند؛ نیاز به `source .env` یا قراردادن توکن در خط Cron نیست. مقدارها تک‌خطی و کامنت‌ها در خط جدا باشند. مسیرهای نسبی نسبت به ریشهٔ پروژه‌اند. پوشهٔ دیتابیس باید برای مالک قابل نوشتن باشد؛ `chmod 777` لازم نیست.

## ۵. آزمایش دستی

نسخه‌های دیگر همین توکن، شامل ویندوز و Railway، را متوقف کنید:

```bash
cd /home/CPANEL_USER/quran-bot
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -u main.py
```

تست نیازمند پوشهٔ `tests/` است. اجرای main باز می‌ماند؛ این رفتار طبیعی است. لاگ `Bot initialized` را بررسی کنید. در تلگرام `/start`، رمز و سپس `/group` را بزنید. بات باید عضو گروه و دارای مجوز ارسال باشد. «ارسال آزمایشی» واقعاً یک پیام به گروه می‌فرستد.

با `Ctrl+C` اجرا را ببندید و صبر کنید کامل خارج شود؛ هنگام فعال‌سازی Cron این نسخه نباید روشن بماند.

## ۶. launcher آماده

`deploy/cpanel_worker.py` از قفل سیستم‌عامل برای جلوگیری از اجرای هم‌زمان در همان پوشه استفاده می‌کند. لاگ `logs/bot.log` با اندازهٔ حدود ۵ مگابایت می‌چرخد و سه فایل قبلی نگه داشته می‌شود. PID در `run/worker.pid` ثبت می‌شود. وجود `run/disabled` مانع شروع تازه است.

قفل، نسخهٔ مستقیم main.py یا میزبان دیگر را پوشش نمی‌دهد. فایل `run/worker.lock` را هنگام اجرا حذف نکنید؛ باقی‌ماندن آن بعد از خروج طبیعی است و قفل واقعی در اختیار سیستم‌عامل است.

آزمایش launcher:

```bash
/home/CPANEL_USER/quran-bot/.venv/bin/python -u /home/CPANEL_USER/quran-bot/deploy/cpanel_worker.py
```

فرمان باز می‌ماند. در Terminal دوم یا File Manager لاگ را ببینید:

```bash
tail -n 60 /home/CPANEL_USER/quran-bot/logs/bot.log
```

پس از آزمایش با `Ctrl+C` ببندید. این فایل محدودیت اجرای دائمی میزبان را برطرف نمی‌کند.

## ۷. Cron Jobs

در cPanel ← Advanced ← Cron Jobs یک کار بسازید. فیلدهای Minute، Hour، Day، Month و Weekday هرکدام `*` باشند. در **Command فقط** این خط را وارد کنید؛ ستاره‌ها را دوباره داخل Command ننویسید:

```bash
/home/CPANEL_USER/quran-bot/.venv/bin/python -u /home/CPANEL_USER/quran-bot/deploy/cpanel_worker.py >> /home/CPANEL_USER/quran-bot/logs/launcher.log 2>&1
```

هر دقیقه یک تلاش برای اجرا انجام می‌شود. اگر بات روشن باشد، تلاش جدید به‌علت قفل خارج می‌شود؛ بات هر دقیقه ری‌استارت نمی‌شود. اگر فرایند خارج شود، نوبت بعدی Cron آن را اجرا می‌کند. این روش پاسخ‌گویی را نمی‌سنجد؛ پردازش هنگ‌کرده ممکن است زنده و قفل‌دار بماند. [Cron و جلوگیری از اجرای هم‌پوشان](https://docs.cpanel.net/cpanel/advanced/cron-jobs/)

`launcher.log` برخلاف bot.log چرخش خودکار ندارد؛ خروجی خارج از logging و خطاهای خیلی زودهنگام را می‌گیرد. اندازه‌اش را بررسی کنید؛ اگر رشد دارد، خطای تکراری را رفع و هنگام توقف فایل را آرشیو کنید. اگر Cron هر دقیقه مجاز نیست، فاصلهٔ مجاز میزبان را انتخاب کنید؛ بازیابی پس از خروج دیرتر خواهد شد.

## ۸. فعال‌کردن مطالعه

۱. تا نوبت بعدی Cron صبر و لاگ را بررسی کنید.
۲. `/start` و رمز را بفرستید.
۳. `/group` ← انتخاب گروه ← بررسی دسترسی ← ارسال آزمایشی.
۴. سوره/صفحه، دوره و ساعت را تنظیم کنید.
۵. «شروع ارسال» را بزنید و زمان ارسال بعدی را ببینید.
۶. پس از موعد، پیام گروه و «آخرین ارسال روزانه» پنل را بررسی کنید.

شروع الزاماً فوری نیست؛ اگر ساعت امروز گذشته باشد نوبت اول معمولاً فرداست. بسته‌شدن مرورگر cPanel نباید فرایند Cron را متوقف کند؛ اگر چنین است، سیاست میزبان را بررسی کنید.

## ۹. توقف و به‌روزرسانی

ابتدا جلوی شروع دوباره را بگیرید:

```bash
touch /home/CPANEL_USER/quran-bot/run/disabled
cat /home/CPANEL_USER/quran-bot/run/worker.pid
ps -u "$(id -un)" -o pid,args
```

PID فایل را با خروجی ps تطبیق دهید؛ فرمانش باید همین مسیر `deploy/cpanel_worker.py` باشد. عدد تأییدشده را جای `VERIFIED_PID` بگذارید:

```bash
kill -TERM VERIFIED_PID
```

PID پس از خروج ناگهانی ممکن است قدیمی باشد؛ بدون تطبیق، فرایند را نکشید. از `pkill python` استفاده نکنید. وجود disabled فقط جلوی شروع تازه را می‌گیرد، نه اجرای روشن را. تا خروج کامل صبر کنید.

بعد از پشتیبان و آپلود کد جدید:

```bash
cd /home/CPANEL_USER/quran-bot
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -q
rm -f /home/CPANEL_USER/quran-bot/run/disabled
```

Cron در نوبت بعدی شروع می‌کند. برای خاموشی دائمی Cron را هم حذف/غیرفعال کنید. «توقف ارسال» داخل بات فقط زمان‌بندی را متوقف می‌کند.

## ۱۰. پشتیبان و انتقال

```bash
cd /home/CPANEL_USER/quran-bot
.venv/bin/python scripts/backup_db.py backups/bot-before-update.db
```

نام خروجی باید جدید باشد؛ فایل موجود بازنویسی نمی‌شود. ابزار از SQLite Backup API استفاده می‌کند. فایل را از File Manager دانلود و خارج از هاست هم نگه دارید؛ `.env` را جداگانه در محل امن نگه دارید.

برای انتقال از ویندوز، نسخهٔ قدیمی را متوقف و در PowerShell پروژه اجرا کنید:

```powershell
.\.venv\Scripts\python.exe scripts/backup_db.py backups\bot-transfer.db
```

وقتی worker مقصد کاملاً متوقف است، snapshot را در مسیر DB_PATH مقصد قرار دهید. برای نصب تازه نام آن `bot_data.db` است. اگر مقصد داده دارد ابتدا پشتیبان بگیرید؛ دیتابیس زنده را بازنویسی نکنید و WAL/SHM متعلق به دیتابیس دیگری را کنار نسخهٔ بازیابی‌شده نگذارید. ترتیب `surah_list.txt` باید با دیتابیس منتقل‌شده یکسان باشد.

ستون‌های جدید در اولین اجرا اضافه می‌شوند. بعد از ری‌استارت رمز دوباره لازم است، ولی گروه و پیشرفت باقی می‌مانند. در پیام «نتیجهٔ ارسال قبلی نامشخص»، ابتدا گروه را بررسی و صفحهٔ صحیح را انتخاب کنید.

## ۱۱. عیب‌یابی

| نشانه | اقدام |
| --- | --- |
| No such file در Cron | Home و مسیر واقعی Python را بررسی کنید |
| ModuleNotFoundError | وابستگی‌ها را با همان Python خط Cron نصب کنید |
| خطای Linux/POSIX | launcher مخصوص Linux است؛ روی ویندوز main.py اجرا کنید |
| خاموش‌شدن پس از مدتی | محدودیت زمان پردازش، RAM، CPU و سیاست میزبان را بپرسید |
| Conflict | نسخهٔ دیگری با همان توکن روشن است |
| Chat not found | مقصد و عضویت بات را در `/group` اصلاح کنید |
| خطای SQLite | فضای دیسک و مجوز پوشهٔ دیتابیس را بررسی کنید |
| Cron هست ولی بات روشن نیست | disabled، لاگ launcher و PID واقعی را بررسی کنید |
| NetworkError | دسترسی خروجی HTTPS و DNS سرور به Telegram را بررسی کنید؛ پروکسی مرورگر اثری روی سرور ندارد |

اجرای واقعی و مجازبودن worker باید روی حساب میزبان شما تأیید شود.
