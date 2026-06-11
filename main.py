"""
LeadQualifier AI - Flask App + GUI
GhostVenumAI | Serkan | April 2026

GUI-Routen:
  GET  /                 - Dashboard
  GET  /leads            - Lead-Verwaltung
  GET  /pipeline         - Pipeline-Ansicht
  GET  /test-webhook     - Lead-Tester
  GET  /settings         - Einstellungen

API-Endpunkte:
  POST /webhook/lead          - Eingehender Lead
  GET  /health                - Health-Check
  GET  /api/status            - Lizenz-Status
  GET  /api/leads/recent      - Letzte Leads
  POST /api/feedback          - Scoring-Feedback
"""

import asyncio
import functools
import hashlib
import hmac
import logging
import os
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from flask import Flask, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from agents.orchestrator import OrchestratorAgent
from core.config import Settings
from core.license import LicenseManager
from core.memory import MemoryDB
from core.models import LeadInput, PipelineResult

# ---------------------------------------------------------------------------
# Logging (kein PII in Logs)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App + Konfiguration
# ---------------------------------------------------------------------------
app = Flask(__name__)

settings = Settings()
app.secret_key = settings.flask_secret_key

# Hinter einem Reverse-Proxy (nginx, Traefik, Load-Balancer) muss
# X-Forwarded-For ausgewertet werden, sonst sieht das Rate-Limiting nur die
# Proxy-IP. TRUSTED_PROXY_COUNT gibt an, wie viele Proxy-Hops vertrauenswuerdig
# sind (0 = kein Proxy, Header wird ignoriert).
if settings.trusted_proxy_count > 0:
    app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
        app.wsgi_app,
        x_for=settings.trusted_proxy_count,
        x_proto=settings.trusted_proxy_count,
        x_host=settings.trusted_proxy_count,
    )

db = MemoryDB(settings.database_url.replace("sqlite:///", ""))
db.init_db()

license_manager = LicenseManager(
    db=db,
    license_server_url=settings.license_server_url,
    demo_max_leads=settings.demo_max_leads,
    demo_max_days=settings.demo_max_days,
)

orchestrator = OrchestratorAgent(settings=settings, db=db)

# Background-Worker fuer den asynchronen Webhook-Modus (202 Accepted).
# Die Pipeline-Ergebnisse landen in SQLite - der Status-Endpoint liest von dort,
# funktioniert also auch mit mehreren Gunicorn-Workern.
_pipeline_executor = ThreadPoolExecutor(
    max_workers=int(os.getenv("PIPELINE_WORKERS", "4")),
    thread_name_prefix="pipeline",
)

# ---------------------------------------------------------------------------
# Rate-Limiting (einfaches In-Memory Dict: IP -> [timestamps])
# ---------------------------------------------------------------------------
_rate_limit_store: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_MAX = 10        # Requests
RATE_LIMIT_WINDOW = 60.0   # Sekunden


def _is_rate_limited(ip: str) -> bool:
    """Prueft ob eine IP das Limit ueberschritten hat (10 Req/Min)."""
    now = time.monotonic()
    timestamps = _rate_limit_store[ip]
    # Alte Eintraege entfernen
    _rate_limit_store[ip] = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    if len(_rate_limit_store[ip]) >= RATE_LIMIT_MAX:
        return True
    _rate_limit_store[ip].append(now)
    return False


# ---------------------------------------------------------------------------
# Webhook-Signatur-Validierung (HMAC-SHA256)
# ---------------------------------------------------------------------------
def _verify_webhook_signature(payload: bytes, signature_header: str | None) -> bool:
    """
    Prueft die HMAC-SHA256 Signatur des Webhooks.

    Args:
        payload: Roher Request-Body.
        signature_header: Wert des X-Webhook-Signature Headers.

    Returns:
        True wenn Signatur korrekt oder kein Secret konfiguriert.
    """
    secret = settings.webhook_secret
    if not secret:
        return True  # Kein Secret konfiguriert -> Validierung deaktiviert
    if not signature_header:
        return False
    expected = hmac.new(
        secret.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature_header)


# ---------------------------------------------------------------------------
# Hilfsfunktion: JSON-Fehlerantwort
# ---------------------------------------------------------------------------
def _error(message: str, status: int) -> tuple[Any, int]:
    """Gibt eine einheitliche JSON-Fehlerantwort zurueck."""
    return jsonify({"success": False, "error": message}), status


# ---------------------------------------------------------------------------
# Admin-Authentifizierung fuer Konfigurations-Endpunkte
# ---------------------------------------------------------------------------
_LOOPBACK_ADDRESSES = {"127.0.0.1", "::1"}


def require_admin(view: Callable) -> Callable:
    """
    Schuetzt Admin-Endpunkte (Settings, Prompts) vor unbefugtem Zugriff.

    Ist ADMIN_TOKEN gesetzt, muss der Request den Header
    'X-Admin-Token: <token>' tragen (Vergleich timing-sicher via
    hmac.compare_digest). Ohne konfigurierten Token sind die Endpunkte
    nur von localhost erreichbar - die App lauscht auf 0.0.0.0, daher
    duerfen Konfigurations-Routen niemals unauthentifiziert ins Netz.

    Args:
        view: Die zu schuetzende Flask-View-Funktion.

    Returns:
        Gewrappte View, die bei fehlender Berechtigung 401/403 liefert.
    """
    @functools.wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> tuple[Any, int]:
        admin_token = os.getenv("ADMIN_TOKEN") or None

        if admin_token:
            provided = request.headers.get("X-Admin-Token", "")
            if not provided or not hmac.compare_digest(provided, admin_token):
                logger.warning("Admin-Endpunkt: Ungueltiger oder fehlender Admin-Token.")
                return _error("Nicht autorisiert.", 401)
            return view(*args, **kwargs)

        # Kein Token konfiguriert: nur localhost zulassen (Fail-Closed nach aussen)
        if request.remote_addr not in _LOOPBACK_ADDRESSES:
            logger.warning(
                "Admin-Endpunkt: Zugriff von %s abgelehnt (kein ADMIN_TOKEN konfiguriert).",
                request.remote_addr,
            )
            return _error(
                "Nicht autorisiert. ADMIN_TOKEN konfigurieren fuer Remote-Zugriff.", 403
            )
        return view(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Background-Pipeline (Async-Webhook-Modus)
# ---------------------------------------------------------------------------
def _run_pipeline_job(
    lead_input: LeadInput,
    lead_id: str,
    license_key: Optional[str],
) -> None:
    """
    Fuehrt die Pipeline im Background-Thread aus (Async-Webhook-Modus).

    Das Ergebnis wird vom Orchestrator in SQLite persistiert; der
    Status-Endpoint /api/leads/<lead_id>/status liest von dort.

    Args:
        lead_input: Validierter Lead aus dem Webhook.
        lead_id: Vorab vergebene Pipeline-UUID (bereits an den Aufrufer
            zurueckgegeben).
        license_key: Lizenzschluessel fuer den Nutzungszaehler.
    """
    try:
        result: PipelineResult = asyncio.run(
            orchestrator.run(lead_input, lead_id=lead_id)
        )
        license_manager.increment_usage(license_key)
        logger.info(
            "Background-Pipeline abgeschlossen: lead_id=%s score=%s",
            lead_id,
            result.score,
        )
    except Exception as exc:
        logger.error(
            "Background-Pipeline fehlgeschlagen fuer lead_id=%s: %s",
            lead_id,
            type(exc).__name__,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# GUI-Routen
# ---------------------------------------------------------------------------

@app.get("/")
def ui_dashboard() -> str:
    """Dashboard-Seite."""
    return render_template("dashboard.html")


@app.get("/leads")
def ui_leads() -> str:
    """Lead-Verwaltungs-Seite."""
    return render_template("leads.html")


@app.get("/pipeline")
def ui_pipeline() -> str:
    """Pipeline-Ansicht (leitet auf Dashboard um)."""
    return render_template("dashboard.html")


@app.get("/test-webhook")
def ui_test_webhook() -> str:
    """Lead-Tester-Seite."""
    return render_template("test_webhook.html")


@app.get("/settings")
def ui_settings() -> str:
    """Einstellungs-Seite."""
    return render_template("settings.html")


# ---------------------------------------------------------------------------
# API: Leads
# ---------------------------------------------------------------------------

@app.get("/api/leads/recent")
def api_leads_recent() -> tuple[Any, int]:
    """Gibt die letzten N verarbeiteten Leads aus der Datenbank zurueck (kein PII)."""
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except ValueError:
        return _error("Ungueltiger limit-Parameter.", 400)
    # GUI erwartet aelteste zuerst (haengt unten an) - DB liefert neueste zuerst
    leads = list(reversed(db.get_recent_leads(limit=limit)))
    return jsonify({"success": True, "leads": leads}), 200


@app.get("/api/leads/<pipeline_id>/status")
def api_lead_status(pipeline_id: str) -> tuple[Any, int]:
    """
    Status-Endpoint fuer den asynchronen Webhook-Modus.

    Liefert "completed" mit dem Ergebnis sobald die Pipeline den Lead in die
    Datenbank geschrieben hat, sonst "processing". Eine unbekannte ID ist von
    "processing" nicht unterscheidbar - Aufrufer sollten ein Client-Timeout
    setzen.
    """
    lead = db.get_lead_by_pipeline_id(pipeline_id)
    if lead is not None:
        return jsonify({"success": True, "status": "completed", "lead": lead}), 200
    return jsonify({"success": True, "status": "processing", "lead": None}), 200


@app.get("/api/costs")
def api_costs() -> tuple[Any, int]:
    """Gibt aggregierte API-Kostenstatistiken aus der Datenbank zurueck."""
    db_stats = db.get_cost_stats()

    today_cost = db_stats.get("cost_today_usd", 0.0)
    today_leads = db_stats.get("leads_today", 0)

    return jsonify({
        "success": True,
        # "session" = heutige Werte (DB-basiert, ueberlebt Neustarts und
        # funktioniert mit mehreren Workern)
        "session": {
            "cost_usd": round(today_cost, 4),
            "leads": today_leads,
            "avg_per_lead": round(today_cost / today_leads, 4) if today_leads else 0.0,
        },
        "total": db_stats,
    }), 200


# ---------------------------------------------------------------------------
# Endpunkte
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> tuple[Any, int]:
    """Health-Check fuer Load-Balancer und Monitoring."""
    return jsonify({"status": "ok", "service": "LeadQualifier AI"}), 200


@app.get("/api/status")
def api_status() -> tuple[Any, int]:
    """Gibt Lizenz-Status und Systeminfo zurueck."""
    license_key = settings.license_key if settings.license_key else None
    license_info = license_manager.check(license_key)
    return jsonify({
        "success": True,
        "license": license_info.model_dump(),
        "qualify_threshold": settings.qualify_threshold,
    }), 200


@app.post("/webhook/lead")
def webhook_lead() -> tuple[Any, int]:
    """
    Haupt-Webhook: Verarbeitet eingehende Leads durch die komplette Pipeline.

    Erwartet JSON:
      { "name": "...", "email": "...", "company": "...", "message": "..." }

    Header:
      X-Webhook-Signature: sha256=<hmac>  (optional, wenn WEBHOOK_SECRET gesetzt)

    Returns:
      JSON mit lead_id, score, email_type, duration_seconds
    """
    # Rate-Limiting
    client_ip = request.remote_addr or "unknown"
    if _is_rate_limited(client_ip):
        logger.warning("Rate-Limit ueberschritten fuer IP: %s", client_ip)
        return _error("Rate-Limit ueberschritten. Maximal 10 Anfragen pro Minute.", 429)

    # Signatur pruefen
    raw_body = request.get_data()
    signature = request.headers.get("X-Webhook-Signature")
    if not _verify_webhook_signature(raw_body, signature):
        logger.warning("Ungueltige Webhook-Signatur")
        return _error("Ungueltige Webhook-Signatur.", 401)

    # JSON parsen
    data = request.get_json(silent=True)
    if not data:
        return _error("Kein gueltiger JSON-Body.", 400)

    # Pydantic-Validierung
    try:
        lead_input = LeadInput(**data)
    except Exception as exc:
        logger.info("Validierungsfehler bei Lead-Input: %s", type(exc).__name__)
        return _error(f"Ungueltige Eingabedaten: {str(exc)}", 422)

    # Lizenz-Check
    license_key = settings.license_key if settings.license_key else None
    license_info = license_manager.check(license_key)
    if not license_info.active:
        logger.info("Lizenz inaktiv: %s", license_info.reason)
        return _error(f"Lizenz inaktiv: {license_info.reason}", 403)

    # Duplikat-Erkennung (Email: 30 Tage, Firma: 7 Tage)
    email_dup = db.check_duplicate_email(str(lead_input.email))
    if email_dup:
        logger.info("Duplikat-Lead abgewiesen: gleiche E-Mail innerhalb von 30 Tagen.")
        return jsonify({
            "success": False,
            "error": "duplicate",
            "reason": "Diese E-Mail-Adresse wurde bereits in den letzten 30 Tagen eingereicht.",
            "last_submitted": email_dup.get("contacted_at"),
            "last_score": email_dup.get("score"),
            "last_status": email_dup.get("status"),
        }), 409

    company_dup = db.check_duplicate_company(lead_input.company)
    if company_dup:
        logger.info("Duplikat-Lead abgewiesen: gleiche Firma innerhalb von 7 Tagen.")
        return jsonify({
            "success": False,
            "error": "duplicate",
            "reason": "Von diesem Unternehmen wurde bereits in den letzten 7 Tagen ein Lead eingereicht.",
            "last_submitted": company_dup.get("contacted_at"),
            "last_score": company_dup.get("score"),
            "last_status": company_dup.get("status"),
        }), 409

    # Async-Modus: sofort 202 zurueckgeben, Pipeline laeuft im Hintergrund.
    # Default kommt aus WEBHOOK_ASYNC_MODE, pro Request via ?async=1 / ?sync=1
    # uebersteuerbar (z.B. GUI-Tester nutzt den synchronen Pfad).
    wants_async = settings.webhook_async_mode
    if request.args.get("async") is not None:
        wants_async = True
    if request.args.get("sync") is not None:
        wants_async = False

    if wants_async:
        lead_id = str(uuid.uuid4())
        _pipeline_executor.submit(_run_pipeline_job, lead_input, lead_id, license_key)
        logger.info("Pipeline im Hintergrund gestartet: lead_id=%s", lead_id)
        return jsonify({
            "success": True,
            "accepted": True,
            "lead_id": lead_id,
            "status_url": f"/api/leads/{lead_id}/status",
        }), 202

    # Synchroner Modus: Pipeline blockierend ausfuehren
    logger.info("Pipeline gestartet fuer Lead (company masked)")
    try:
        result: PipelineResult = asyncio.run(orchestrator.run(lead_input))
    except Exception as exc:
        logger.error("Pipeline-Fehler: %s", type(exc).__name__)
        return _error("Interner Pipeline-Fehler.", 500)

    # Lead-Zaehler erhoehen
    license_manager.increment_usage(license_key)

    # Onboarding-Mail nach erstem Lead (Demo)
    onboarding_msg = None
    if license_info.type == "demo" and license_info.leads_remaining is not None:
        used = settings.demo_max_leads - license_info.leads_remaining
        if used == 0:
            onboarding_msg = license_manager.get_onboarding_message(
                leads_remaining=license_info.leads_remaining - 1,
                days_remaining=license_info.days_remaining or 0,
            )

    response_data: dict[str, Any] = {
        "success": not bool(result.error),
        "lead_id": result.lead_id,
        "score": result.score,
        "email_type": result.email_type,
        "email_sent": result.email_sent,
        "duration_seconds": round(result.duration_seconds, 2),
    }
    if result.error:
        response_data["error"] = result.error
    if onboarding_msg:
        response_data["onboarding_message"] = onboarding_msg

    # Immer 200: Teil-Fehler (z.B. E-Mail nicht gesendet) stehen in
    # email_sent/error im Body. 207 Multi-Status waere hier semantisch falsch.
    logger.info("Pipeline abgeschlossen: lead_id=%s score=%s", result.lead_id, result.score)
    return jsonify(response_data), 200


@app.get("/api/settings/config")
@require_admin
def api_settings_config() -> tuple[Any, int]:
    """
    Gibt den Konfigurationsstatus fuer die Settings-UI zurueck.

    Es werden KEINE Secret-Inhalte zurueckgegeben - auch keine Praefixe.
    Fuer jeden Secret-Wert wird nur signalisiert, ob er gesetzt ist.
    """
    def status(env_key: str) -> str | None:
        """Gibt 'konfiguriert ✓' zurueck wenn der Wert gesetzt ist, sonst None."""
        return "konfiguriert ✓" if os.getenv(env_key) else None

    return jsonify({
        "success": True,
        "config": {
            "anthropic_api_key":    status("ANTHROPIC_API_KEY"),
            "slack_webhook_url":    status("SLACK_WEBHOOK_URL"),
            "slack_encryption_key": status("SLACK_ENCRYPTION_KEY"),
            "gmail_sender_email":   status("GMAIL_SENDER_EMAIL"),
            "gmail_app_password":   status("GMAIL_APP_PASSWORD"),
            "email_override":       os.getenv("EMAIL_OVERRIDE") or None,
            "crm_webhook_url":      status("CRM_WEBHOOK_URL"),
            "webhook_secret":       status("WEBHOOK_SECRET"),
            "license_key":          status("LICENSE_KEY"),
            "admin_token":          status("ADMIN_TOKEN"),
            "qualify_threshold":    os.getenv("QUALIFY_THRESHOLD", "7"),
        },
    }), 200


@app.post("/api/settings/save")
@require_admin
def api_settings_save() -> tuple[Any, int]:
    """
    Speichert neue Konfigurationswerte in die .env-Datei.

    Erlaubte Keys sind explizit gelistet (Whitelist).
    Leere Strings werden ignoriert (kein Loeschen von Keys ueber die UI).
    """
    from dotenv import set_key

    _ALLOWED_KEYS: set[str] = {
        "ANTHROPIC_API_KEY",
        "SLACK_WEBHOOK_URL",
        "SLACK_ENCRYPTION_KEY",
        "GMAIL_SENDER_EMAIL",
        "GMAIL_APP_PASSWORD",
        "EMAIL_OVERRIDE",
        "CRM_WEBHOOK_URL",
        "WEBHOOK_SECRET",
        "LICENSE_KEY",
        "ADMIN_TOKEN",
        "QUALIFY_THRESHOLD",
    }

    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return _error("Kein gueltiger JSON-Body.", 400)

    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    saved: list[str] = []

    for key, value in data.items():
        if key not in _ALLOWED_KEYS:
            continue
        if not isinstance(value, str) or not value.strip():
            continue

        value = value.strip()

        # QUALIFY_THRESHOLD: nur Zahlen 1-10
        if key == "QUALIFY_THRESHOLD":
            try:
                int_val = int(value)
                if not 1 <= int_val <= 10:
                    continue
                value = str(int_val)
            except ValueError:
                continue

        try:
            set_key(env_path, key, value)
            os.environ[key] = value  # Fuer die aktuelle Session sofort wirksam
            saved.append(key)
            logger.info("Einstellung gespeichert: %s", key)
        except Exception as exc:
            logger.error("Fehler beim Speichern von %s: %s", key, type(exc).__name__)

    if not saved:
        return _error("Keine gueltigen Felder zum Speichern.", 400)

    return jsonify({"success": True, "saved": saved}), 200


@app.post("/api/feedback")
def api_feedback() -> tuple[Any, int]:
    """
    Speichert Scoring-Feedback fuer das Lern-System.

    Erwartet JSON:
      { "lead_id": 1, "actual_outcome": "converted" | "rejected" | "no_response" }
    """
    client_ip = request.remote_addr or "unknown"
    if _is_rate_limited(client_ip):
        return _error("Rate-Limit ueberschritten. Maximal 10 Anfragen pro Minute.", 429)

    data = request.get_json(silent=True)
    if not data:
        return _error("Kein gueltiger JSON-Body.", 400)

    lead_id = data.get("lead_id")
    actual_outcome = data.get("actual_outcome")

    if not lead_id or not actual_outcome:
        return _error("lead_id und actual_outcome sind Pflichtfelder.", 400)

    valid_outcomes = {"converted", "rejected", "no_response"}
    if actual_outcome not in valid_outcomes:
        return _error(f"actual_outcome muss einer von {valid_outcomes} sein.", 422)

    try:
        db.save_scoring_feedback(
            lead_id=int(lead_id),
            actual_outcome=actual_outcome,
        )
    except Exception as exc:
        logger.error("Fehler beim Speichern des Feedbacks: %s", type(exc).__name__)
        return _error("Fehler beim Speichern.", 500)

    return jsonify({"success": True, "message": "Feedback gespeichert."}), 200


@app.get("/api/prompts")
@require_admin
def api_prompts_get() -> tuple[Any, int]:
    """Gibt alle konfigurierten Agent-System-Prompts zurueck."""
    from core import prompt_manager as pm
    return jsonify({"success": True, "prompts": pm.get_all()}), 200


@app.post("/api/prompts")
@require_admin
def api_prompts_save() -> tuple[Any, int]:
    """
    Speichert einen oder mehrere Agent-System-Prompts.

    Erwartet JSON: { "key": "scoring_system", "content": "..." }
    oder           { "prompts": { "scoring_system": "...", ... } }
    """
    from core import prompt_manager as pm

    data = request.get_json(silent=True)
    if not data:
        return _error("Kein gueltiger JSON-Body.", 400)

    valid_keys = set(pm.DEFAULTS.keys())

    # Einzel-Speicherung: { key, content }
    if "key" in data and "content" in data:
        key = data["key"]
        if key not in valid_keys:
            return _error(f"Unbekannter Prompt-Key: {key}", 400)
        ok = pm.save(key, data["content"])
        return jsonify({"success": ok}), 200 if ok else 500

    # Bulk-Speicherung: { prompts: { key: content, ... } }
    if "prompts" in data and isinstance(data["prompts"], dict):
        saved, failed = [], []
        for key, content in data["prompts"].items():
            if key not in valid_keys:
                continue
            if pm.save(key, content):
                saved.append(key)
            else:
                failed.append(key)
        return jsonify({"success": not failed, "saved": saved, "failed": failed}), 200

    # Reset auf Default: { "key": "...", "reset": true }
    if data.get("reset"):
        key = data.get("key")
        ok = pm.reset(key if key in valid_keys else None)
        return jsonify({"success": ok}), 200 if ok else 500

    return _error("Ungueltige Anfrage.", 400)


# ---------------------------------------------------------------------------
# App starten
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    missing = settings.validate_required()
    if missing:
        logger.warning("Fehlende Konfiguration: %s", missing)

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
    )
