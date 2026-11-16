FROM python:3.12-slim

WORKDIR /app

# System libs for lxml wheels are bundled; no apt needed for slim + wheels.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY frontend ./frontend

# Writable dir for the SQLite db (ephemeral on free hosts — prefs reseed from env,
# and the current booking is re-detected from the live schedule page on restart).
RUN mkdir -p /app/data
ENV DB_PATH=/app/data/fiteasy.db
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
