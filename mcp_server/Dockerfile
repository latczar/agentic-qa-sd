FROM python:3.12-slim

WORKDIR /app

COPY mcp_server/requirements.txt mcp_server/requirements.txt
RUN pip install --no-cache-dir -r mcp_server/requirements.txt

COPY shared/ shared/
COPY mcp_server/ mcp_server/

ENV PYTHONPATH=/app
WORKDIR /app/mcp_server

CMD ["python", "-m", "app.server"]
