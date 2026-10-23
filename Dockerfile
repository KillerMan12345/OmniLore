FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY omnilore ./omnilore
COPY sql ./sql

RUN pip install --no-cache-dir -e .

ENV PYTHONUNBUFFERED=1 \
    OMNILORE_DATA_DIR=/data/OmniLore_Data

VOLUME ["/data/OmniLore_Data"]

CMD ["python3", "-m", "omnilore.mcp_stdio_server"]
