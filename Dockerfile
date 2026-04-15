FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=38104 \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PATH=/mcp

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip hatchling && \
    pip install --no-cache-dir .

EXPOSE 38104

CMD ["intervals-gateway"]
