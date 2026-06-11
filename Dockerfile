# LeadQualifier AI - Produktions-Image
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY core/ core/
COPY agents/ agents/
COPY integrations/ integrations/
COPY templates/ templates/
COPY main.py gunicorn.conf.py ./

# Non-root User; /app/data ist das Volume fuer SQLite + prompts.json
RUN useradd --create-home --uid 1000 leadqualifier \
    && mkdir -p /app/data \
    && chown -R leadqualifier:leadqualifier /app
USER leadqualifier

ENV DATABASE_URL=sqlite:////app/data/leadqualifier.db

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=3)"

CMD ["gunicorn", "-c", "gunicorn.conf.py", "main:app"]
