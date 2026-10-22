# The agent as a 24/7 paper trader on a server or cloud host (Railway, any VPS with Docker).
# Everything it keeps (portfolio, trade journal, hourly trade CSVs) is written under /data:
# attach a persistent volume there or each redeploy starts from a fresh $1,000 paper wallet.
# Keys come from environment variables; see .env.example.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY memecoin_agent ./memecoin_agent
RUN pip install . && rm -rf /app/memecoin_agent
COPY deploy/server.yaml /app/server.yaml

WORKDIR /data
CMD ["memecoin-agent", "-c", "/app/server.yaml", "run"]
