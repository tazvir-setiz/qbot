FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/bot_data.db

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py surah_list.txt ./
COPY quran_bot/ ./quran_bot/
COPY scripts/ ./scripts/

CMD ["python", "-u", "main.py"]
