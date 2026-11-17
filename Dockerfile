FROM python:3.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CINEVAULT_DATA_DIR=/data

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium nodejs npm ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY cinevault ./cinevault
COPY scripts ./scripts
COPY package.json package-lock.json pyproject.toml README.md ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund

RUN useradd --system --uid 10001 --home-dir /data --create-home cinevault \
    && chown -R cinevault:cinevault /data /app
USER cinevault

EXPOSE 8765
VOLUME ["/data"]
CMD ["uvicorn", "cinevault.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765", "--workers", "1", "--no-proxy-headers"]
