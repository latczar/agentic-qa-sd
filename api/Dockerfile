FROM python:3.12-slim

WORKDIR /app

COPY api/requirements.txt api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt

COPY shared/ shared/
COPY api/ api/

ENV PYTHONPATH=/app
WORKDIR /app/api

# --timeout-graceful-shutdown: the overseer holds a Server-Sent Events stream
# open for as long as its tab is open, and uvicorn's graceful shutdown waits for
# every connection to close. Without a limit, a --reload while that page was open
# waited forever and the API stopped answering, /health included. The page
# reconnects on its own, so cutting the stream after a few seconds costs nothing.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--timeout-graceful-shutdown", "3"]
