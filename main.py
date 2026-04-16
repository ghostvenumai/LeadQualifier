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
import hashlib
import hmac
import logging
import time
from collections import defaultdict
from typing import Any

from flask import Flask, jsonify, render_template, request

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

db = MemoryDB(settings.database_url.replace("sqlite:///", ""))
db.init_db()

license_manager = LicenseManager(
    db=db,
    license_server_url=settings.license_server_url,
    demo_max_leads=settings.demo_max_leads,
    demo_max_days=settings.demo_max_days,
)

orchestrator = OrchestratorAgent(settings=settings)

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


# In-Memory Lead-Store fuer GUI (wird bei Neustart geleert — DB ist die Quelle)
_recent_leads: list[dict] = []


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
    """Gibt die letzten N verarbeiteten Leads zurueck (kein PII)."""
    limit = min(int(request.args.get("limit", 50)), 200)
    return jsonify({"success": True, "leads": _recent_leads[-limit:]}), 200


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

    # Pipeline starten
    logger.info("Pipeline gestartet fuer Lead (company masked)")
    try:
        result: PipelineResult = asyncio.run(orchestrator.run(lead_input))
    except Exception as exc:
        logger.error("Pipeline-Fehler: %s", type(exc).__name__)
        return _error("Interner Pipeline-Fehler.", 500)

    # Lead-Zaehler erhoehen
    license_manager.increment_usage(license_key)

    # GUI-Store aktualisieren (kein PII — nur company, score, type)
    from datetime import datetime as _dt
    _recent_leads.append({
        "lead_id": result.lead_id,
        "company": lead_input.company,
        "score": result.score,
        "email_type": result.email_type,
        "email_sent": result.email_sent,
        "duration_seconds": round(result.duration_seconds, 2),
        "created_at": _dt.now().isoformat(),
        "error": result.error,
    })
    if len(_recent_leads) > 500:
        _recent_leads.pop(0)

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

    status_code = 200 if result.email_sent else 207
    logger.info("Pipeline abgeschlossen: lead_id=%s score=%s", result.lead_id, result.score)
    return jsonify(response_data), status_code


@app.get("/api/settings/config")
def api_settings_config() -> tuple[Any, int]:
    """
    Gibt maskierte Konfigurationswerte fuer die Settings-UI zurueck.

    Keine echten Secrets werden zurueckgegeben — nur ob ein Wert gesetzt ist
    und die ersten Zeichen fuer die visuelle Bestaetigung.
    """
    import os

    def mask(value: str | None, show: int = 8) -> str | None:
        """Maskiert einen Secret-Wert: zeigt nur die ersten `show` Zeichen."""
        if not value:
            return None
        if len(value) <= show:
            return "*" * len(value)
        return value[:show] + "…"

    return jsonify({
        "success": True,
        "config": {
            "anthropic_api_key":    mask(os.getenv("ANTHROPIC_API_KEY")),
            "slack_webhook_url":    mask(os.getenv("SLACK_WEBHOOK_URL"), 30),
            "slack_encryption_key": mask(os.getenv("SLACK_ENCRYPTION_KEY")),
            "gmail_sender_email":   mask(os.getenv("GMAIL_SENDER_EMAIL"), 20),
            "gmail_app_password":   mask(os.getenv("GMAIL_APP_PASSWORD")),
            "crm_webhook_url":      mask(os.getenv("CRM_WEBHOOK_URL"), 30),
            "webhook_secret":       mask(os.getenv("WEBHOOK_SECRET")),
            "license_key":          mask(os.getenv("LICENSE_KEY")),
            "qualify_threshold":    os.getenv("QUALIFY_THRESHOLD", "7"),
        },
    }), 200


@app.post("/api/settings/save")
def api_settings_save() -> tuple[Any, int]:
    """
    Speichert neue Konfigurationswerte in die .env-Datei.

    Erlaubte Keys sind explizit gelistet (Whitelist).
    Leere Strings werden ignoriert (kein Loeschen von Keys ueber die UI).
    """
    import os
    from dotenv import set_key

    _ALLOWED_KEYS: set[str] = {
        "ANTHROPIC_API_KEY",
        "SLACK_WEBHOOK_URL",
        "SLACK_ENCRYPTION_KEY",
        "GMAIL_SENDER_EMAIL",
        "GMAIL_APP_PASSWORD",
        "CRM_WEBHOOK_URL",
        "WEBHOOK_SECRET",
        "LICENSE_KEY",
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
            predicted_score=0,  # wird aus DB geladen
            actual_outcome=actual_outcome,
        )
    except Exception as exc:
        logger.error("Fehler beim Speichern des Feedbacks: %s", type(exc).__name__)
        return _error("Fehler beim Speichern.", 500)

    return jsonify({"success": True, "message": "Feedback gespeichert."}), 200


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
