"""
Gunicorn-Konfiguration fuer LeadQualifier AI (Produktion).

Start: gunicorn -c gunicorn.conf.py main:app

Worker-Modell: sync-Worker mit Threads. Die Pipeline laeuft im synchronen
Webhook-Modus bis zu ~60s - dafuer ist der Timeout grosszuegig gesetzt.
Fuer Produktionslast den Async-Modus aktivieren (WEBHOOK_ASYNC_MODE=true),
dann antwortet der Webhook sofort mit 202.
"""

import multiprocessing
import os

bind = os.getenv("GUNICORN_BIND", "0.0.0.0:5000")

# SQLite vertraegt keine hohe Schreib-Parallelitaet - wenige Worker mit
# Threads sind hier der richtige Kompromiss.
workers = int(os.getenv("GUNICORN_WORKERS", str(min(2, multiprocessing.cpu_count()))))
threads = int(os.getenv("GUNICORN_THREADS", "4"))

# Synchroner Pipeline-Durchlauf kann ~60s dauern
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info").lower()

# Keine Request-Bodies/Header mit PII ins Access-Log
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(L)ss'
