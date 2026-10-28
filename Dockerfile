FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent data volume (Backup Browser index / cache)
VOLUME ["/data"]
RUN mkdir -p /tmp/tg_dl

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ.get('PORT','8080')+'/healthz',timeout=4)" || exit 1

CMD ["python", "app.py"]
