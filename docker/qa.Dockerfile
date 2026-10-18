FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm git \
    && pip install --no-cache-dir pytest pytest-asyncio playwright \
    && PLAYWRIGHT_BROWSERS_PATH=/opt/browsers playwright install --with-deps chromium \
    && chmod -R a+rX /opt/browsers \
    && rm -rf /var/lib/apt/lists/*
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /workspace
USER 65534:65534
