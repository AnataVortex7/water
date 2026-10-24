FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1
# 1 worker: PC यादी + bot state memory मध्ये आहे. 32 threads: दर PC एक long-poll connection धरतो.
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:${PORT:-10000} -w 1 --threads 32 --timeout 90 app:app"]
