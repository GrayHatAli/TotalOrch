FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 totalorch \
    && mkdir -p /var/lib/total-orch \
    && chown -R totalorch:totalorch /app /var/lib/total-orch

USER totalorch

EXPOSE 8080
ENTRYPOINT ["total-orch"]
