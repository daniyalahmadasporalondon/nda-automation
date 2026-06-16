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

# The [tables] extra adds camelot `stream` table recovery for borderless 2-column
# signature/notice/term blocks (NDA_TABLE_AUGMENTATION_ENABLED, on in render.yaml).
# It pulls opencv-python-headless + pandas + numpy (~223MB resident). camelot is
# LAZY-imported in nda_automation.table_extraction — only when a keyword-gated page
# is actually processed — so the resident cost stays deferred and bounded, and the
# feature no-ops cleanly if the import ever fails.
RUN python -m pip install --upgrade pip \
    && python -m pip install ".[pdf,gmail,tables]"

CMD ["sh", "-c", "python -m nda_automation.server --host 0.0.0.0 --port ${PORT:-8787}"]
