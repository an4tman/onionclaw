FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY so_gateway/ ./so_gateway/

RUN pip install --no-cache-dir .

# Audit/undo DB default location (SO_AUDIT_DB). Mount a volume here to persist
# the tuning audit log across container recreates.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

CMD ["python", "-m", "so_gateway.server"]
