"""
Konfigurationsmodul fuer LeadQualifier AI.

Laedt alle Umgebungsvariablen aus der .env-Datei und stellt sie
als typisierte Settings-Instanz bereit. Validiert kritische Werte
beim Laden.

Verwendung:
    from core.config import settings
    api_key = settings.anthropic_api_key
"""

import os
import logging
from functools import cached_property
from typing import Optional

from dotenv import load_dotenv

# .env aus dem Projektroot laden (eine Ebene ueber core/)
load_dotenv()

logger = logging.getLogger(__name__)


class Settings:
    """
    Zentrale Konfigurationsklasse fuer alle Umgebungsvariablen.

    Laedt Werte aus Umgebungsvariablen (via .env oder direkt gesetzt).
    Sensible Werte werden NICHT geloggt. Fehlende Pflichtfelder werden
    beim ersten Zugriff durch property-Methoden erkannt.

    Beispiel:
        from core.config import settings
        print(settings.qualify_threshold)  # 7
    """

    # -------------------------------------------------------------------------
    # ANTHROPIC / AI
    # -------------------------------------------------------------------------

    @cached_property
    def anthropic_api_key(self) -> str:
        """
        Anthropic API Key fuer den Zugriff auf Claude-Modelle.

        Raises:
            EnvironmentError: Wenn der Key nicht gesetzt ist.
        """
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY ist nicht gesetzt. "
                "Bitte in der .env-Datei konfigurieren."
            )
        return key

    @cached_property
    def claude_writer_model(self) -> str:
        """Claude-Modell fuer den Writer Agent (Opus fuer beste Qualitaet)."""
        return os.getenv("CLAUDE_WRITER_MODEL", "claude-opus-4-6")

    @cached_property
    def claude_agent_model(self) -> str:
        """Claude-Modell fuer Research, Scoring und Reviewer Agents."""
        return os.getenv("CLAUDE_AGENT_MODEL", "claude-sonnet-4-6")

    # -------------------------------------------------------------------------
    # SLACK
    # -------------------------------------------------------------------------

    @cached_property
    def slack_webhook_url(self) -> Optional[str]:
        """
        Slack Incoming Webhook URL fuer Alert-Benachrichtigungen.
        Optional - wenn nicht gesetzt, werden keine Slack-Alerts gesendet.
        """
        return os.getenv("SLACK_WEBHOOK_URL") or None

    @cached_property
    def slack_encryption_key(self) -> Optional[bytes]:
        """
        32-Byte AES-256-GCM Verschluesselungskey fuer Slack-Payloads.

        Muss als Hex-String (64 Zeichen) oder Base64 in der .env gesetzt sein.
        Optional - wenn nicht gesetzt, werden Slack-Messages nicht verschluesselt.

        Returns:
            32 Bytes als bytes-Objekt oder None wenn nicht konfiguriert.
        """
        raw = os.getenv("SLACK_ENCRYPTION_KEY", "")
        if not raw:
            return None
        try:
            # Hex-encoded (64 chars) oder direkt als bytes
            if len(raw) == 64:
                return bytes.fromhex(raw)
            # Base64-encoded (44 chars)
            import base64
            decoded = base64.b64decode(raw)
            if len(decoded) == 32:
                return decoded
            logger.warning("SLACK_ENCRYPTION_KEY hat ungueltiges Format, wird ignoriert.")
            return None
        except (ValueError, Exception):
            logger.warning("SLACK_ENCRYPTION_KEY konnte nicht dekodiert werden.")
            return None

    # -------------------------------------------------------------------------
    # GMAIL / EMAIL
    # -------------------------------------------------------------------------

    @cached_property
    def gmail_credentials_json(self) -> Optional[str]:
        """
        Pfad zur Gmail OAuth2 credentials.json Datei.
        Optional - wenn nicht gesetzt, wird kein E-Mail-Versand durchgefuehrt.
        """
        return os.getenv("GMAIL_CREDENTIALS_JSON") or None

    @cached_property
    def gmail_sender_email(self) -> Optional[str]:
        """
        Absender-E-Mail-Adresse fuer ausgehende Leads-E-Mails.
        """
        return os.getenv("GMAIL_SENDER_EMAIL") or None

    @cached_property
    def gmail_app_password(self) -> Optional[str]:
        """
        Google App-Passwort fuer Gmail SMTP.
        Unter: Google-Konto -> Sicherheit -> App-Passwoerter generieren.
        16 Zeichen, ohne Leerzeichen eintragen.
        """
        return os.getenv("GMAIL_APP_PASSWORD") or None

    @cached_property
    def email_override(self) -> Optional[str]:
        """
        Demo-Modus: Alle ausgehenden E-Mails werden an diese Adresse umgeleitet
        statt an die echte Lead-E-Mail. Ideal fuer Vorfuehrungen und Tests.
        """
        return os.getenv("EMAIL_OVERRIDE") or None

    # -------------------------------------------------------------------------
    # CRM INTEGRATION
    # -------------------------------------------------------------------------

    @cached_property
    def crm_webhook_url(self) -> Optional[str]:
        """
        Webhook URL des externen CRM-Systems (z.B. HubSpot, Pipedrive).
        Optional - wenn nicht gesetzt, wird kein CRM-Update durchgefuehrt.
        """
        return os.getenv("CRM_WEBHOOK_URL") or None

    # -------------------------------------------------------------------------
    # LIZENZ
    # -------------------------------------------------------------------------

    @cached_property
    def license_key(self) -> Optional[str]:
        """
        Lizenzschluessel fuer LeadQualifier AI.
        Wenn nicht gesetzt, laeuft das System im Demo-Modus.
        """
        return os.getenv("LICENSE_KEY") or None

    @cached_property
    def license_server_url(self) -> str:
        """
        URL des Lizenz-Validierungsservers.
        Default: GhostVenumAI Lizenzserver.
        """
        return os.getenv(
            "LICENSE_SERVER_URL",
            "https://license.ghostvenumai.de/validate"
        )

    # -------------------------------------------------------------------------
    # DATENBANK
    # -------------------------------------------------------------------------

    @cached_property
    def database_url(self) -> str:
        """
        SQLite Datenbankpfad oder andere Datenbank-URL.
        Default: lokale SQLite-Datei im Projektroot.
        """
        return os.getenv("DATABASE_URL", "sqlite:///leadqualifier.db")

    # -------------------------------------------------------------------------
    # FLASK / WEB
    # -------------------------------------------------------------------------

    @cached_property
    def flask_secret_key(self) -> str:
        """
        Flask Secret Key fuer Session-Sicherheit.

        Raises:
            EnvironmentError: Wenn kein sicherer Key gesetzt ist (in Produktion).
        """
        key = os.getenv("FLASK_SECRET_KEY", "")
        if not key:
            logger.warning(
                "FLASK_SECRET_KEY nicht gesetzt. "
                "Generiere temporaeren Key - nur fuer Entwicklung geeignet!"
            )
            import secrets
            return secrets.token_hex(32)
        return key

    @cached_property
    def webhook_secret(self) -> Optional[str]:
        """
        Shared Secret zur Validierung eingehender Webhook-Requests (HMAC).
        Optional - wenn nicht gesetzt, werden Webhooks nicht signiert geprueft.
        """
        return os.getenv("WEBHOOK_SECRET") or None

    # -------------------------------------------------------------------------
    # LOGGING
    # -------------------------------------------------------------------------

    @cached_property
    def log_level(self) -> str:
        """
        Log-Level fuer das gesamte System.
        Gueltge Werte: DEBUG, INFO, WARNING, ERROR, CRITICAL.
        """
        level = os.getenv("LOG_LEVEL", "INFO").upper()
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in valid_levels:
            logger.warning(
                "Ungueltiger LOG_LEVEL '%s'. Fallback auf INFO.", level
            )
            return "INFO"
        return level

    # -------------------------------------------------------------------------
    # DEMO-LIMITS
    # -------------------------------------------------------------------------

    @cached_property
    def demo_max_leads(self) -> int:
        """
        Maximale Anzahl Leads im Demo-Modus.
        Default: 10 Leads.
        """
        try:
            value = int(os.getenv("DEMO_MAX_LEADS", "10"))
            if value < 1:
                raise ValueError("DEMO_MAX_LEADS muss mindestens 1 sein.")
            return value
        except ValueError as exc:
            logger.warning("Ungueltiger DEMO_MAX_LEADS-Wert: %s", exc)
            return 10

    @cached_property
    def demo_max_days(self) -> int:
        """
        Maximale Laufzeit in Tagen im Demo-Modus.
        Default: 14 Tage.
        """
        try:
            value = int(os.getenv("DEMO_MAX_DAYS", "14"))
            if value < 1:
                raise ValueError("DEMO_MAX_DAYS muss mindestens 1 sein.")
            return value
        except ValueError as exc:
            logger.warning("Ungueltiger DEMO_MAX_DAYS-Wert: %s", exc)
            return 14

    # -------------------------------------------------------------------------
    # PIPELINE-KONFIGURATION
    # -------------------------------------------------------------------------

    @cached_property
    def qualify_threshold(self) -> int:
        """
        Mindest-Score ab dem ein Lead als qualifiziert gilt.
        Default: 7 (von 10).
        """
        try:
            value = int(os.getenv("QUALIFY_THRESHOLD", "7"))
            if not 1 <= value <= 10:
                raise ValueError("QUALIFY_THRESHOLD muss zwischen 1 und 10 liegen.")
            return value
        except ValueError as exc:
            logger.warning("Ungueltiger QUALIFY_THRESHOLD-Wert: %s", exc)
            return 7

    # -------------------------------------------------------------------------
    # VALIDIERUNGS-HELPER
    # -------------------------------------------------------------------------

    def validate_required(self) -> list[str]:
        """
        Prueft alle Pflichtfelder und gibt eine Liste fehlender Felder zurueck.

        Gibt alle Fehler zurueck ohne Exceptions zu werfen, sodass ein
        vollstaendiger Bericht moeglich ist.

        Returns:
            Liste der Namen fehlender/ungueltige Pflichtfelder.
        """
        missing: list[str] = []

        if not os.getenv("ANTHROPIC_API_KEY"):
            missing.append("ANTHROPIC_API_KEY")

        # Gmail: wenn eines gesetzt ist, muss das andere auch gesetzt sein
        has_credentials = bool(os.getenv("GMAIL_CREDENTIALS_JSON"))
        has_sender = bool(os.getenv("GMAIL_SENDER_EMAIL"))
        if has_credentials != has_sender:
            missing.append(
                "GMAIL_CREDENTIALS_JSON + GMAIL_SENDER_EMAIL "
                "(beide oder keines muss gesetzt sein)"
            )

        return missing

    def is_demo_mode(self) -> bool:
        """
        Gibt True zurueck wenn das System im Demo-Modus laeuft.

        Returns:
            True wenn kein LICENSE_KEY gesetzt ist.
        """
        return not bool(os.getenv("LICENSE_KEY"))

    def get_safe_summary(self) -> dict:
        """
        Gibt eine sichere Zusammenfassung der Konfiguration zurueck.

        Keine sensiblen Werte (API-Keys, Secrets, E-Mail-Adressen) werden
        zurueckgegeben. Geeignet fuer Startup-Logging.

        Returns:
            Dictionary mit nicht-sensiblen Konfigurationswerten.
        """
        return {
            "demo_mode": self.is_demo_mode(),
            "demo_max_leads": self.demo_max_leads,
            "demo_max_days": self.demo_max_days,
            "qualify_threshold": self.qualify_threshold,
            "log_level": self.log_level,
            "database_url": self.database_url,
            "license_server_url": self.license_server_url,
            "slack_enabled": self.slack_webhook_url is not None,
            "slack_encrypted": self.slack_encryption_key is not None,
            "gmail_enabled": self.gmail_credentials_json is not None,
            "crm_enabled": self.crm_webhook_url is not None,
            "anthropic_key_set": bool(os.getenv("ANTHROPIC_API_KEY")),
            "writer_model": self.claude_writer_model,
            "agent_model": self.claude_agent_model,
        }


# Singleton-Instanz fuer den Import
settings = Settings()
