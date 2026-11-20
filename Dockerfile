FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# عامل واحد (لأن سجل المحادثات في الذاكرة) مع 4 threads لتحمّل عدة طلبات معاً بذاكرة قليلة.
# المهلة 90 ثانية لأن طلب Gemini قد يستغرق حتى ~60 ثانية مع إعادة المحاولة.
CMD exec gunicorn --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 4 --timeout 90 --access-logfile - app:app
