FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fontconfig \
        libreoffice-writer \
        fonts-crosextra-caladea \
        fonts-crosextra-carlito \
        fonts-dejavu \
        fonts-liberation \
        fonts-noto-core \
    && fc-cache -f \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY nda_automation ./nda_automation
COPY static ./static
COPY playbook.json ./

RUN python -m pip install --upgrade pip \
    && python -m pip install ".[pdf,gmail]"

CMD ["sh", "-c", "python -m nda_automation.server --host 0.0.0.0 --port ${PORT:-8787}"]
