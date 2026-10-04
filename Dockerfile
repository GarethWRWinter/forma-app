FROM python:3.13-slim

WORKDIR /app

# Install system deps for psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

# Pinned: a library release must never change production on its own.
COPY pyproject.toml constraints.txt ./
RUN pip install --no-cache-dir -c constraints.txt .

COPY . .

EXPOSE 8000

# A failed migration stops here, so the deploy fails and the previous one
# keeps serving, instead of the app starting against the old schema.
CMD ["sh", "-c", "alembic upgrade head && echo 'Migrations complete, starting server on port ${PORT:-8000}' && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --log-level info"]
