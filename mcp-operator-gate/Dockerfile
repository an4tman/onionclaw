FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY operator_gate/ ./operator_gate/

RUN pip install --no-cache-dir .

# Persists the 'handled' store (HANDLED_DB) across recreates.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

CMD ["python", "-m", "operator_gate.server"]
