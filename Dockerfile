# Playwright image ships Chromium + all system deps, for amd64 and arm64.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    TZ=Australia/Sydney

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

# Single worker on purpose: scraping jobs run on one in-process queue.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
