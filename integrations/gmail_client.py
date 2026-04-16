"""
GmailClient: E-Mail-Versand ueber Gmail SMTP mit App-Passwort.

Keine Google Cloud Console noetig — nur Gmail-Adresse + App-Passwort.
E-Mail-Adressen und Body-Inhalte werden NICHT geloggt (DSGVO).

Setup:
    1. Google-Konto -> Sicherheit -> 2-Schritt-Verifizierung aktivieren
    2. Google-Konto -> Sicherheit -> App-Passwoerter -> "Mail" -> Generieren
    3. 16-stelligen Code in .env als GMAIL_APP_PASSWORD eintragen
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from core.blackboard import LeadBlackboard

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


class GmailClient:
    """E-Mail-Versand via Gmail SMTP mit App-Passwort."""

    def __init__(self, sender_email: str, app_password: str) -> None:
        """
        Args:
            sender_email: Gmail-Adresse des Absenders.
            app_password: Google App-Passwort (16 Zeichen, keine Leerzeichen).
        """
        self.sender_email = sender_email
        self._app_password = app_password

    def _build_message(
        self,
        to: str,
        subject: str,
        body: str,
    ) -> MIMEMultipart:
        """Baut die MIME-E-Mail auf."""
        msg = MIMEMultipart("alternative")
        msg["From"] = f"LeadQualifier AI <{self.sender_email}>"
        msg["To"] = to
        msg["Subject"] = subject

        # Plain-Text Version
        msg.attach(MIMEText(body, "plain", "utf-8"))

        # HTML-Version (einfaches Styling)
        html_body = self._to_html(body)
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        return msg

    def _to_html(self, text: str) -> str:
        """Konvertiert Plain-Text in einfaches HTML."""
        lines = text.split("\n")
        html_lines = []
        for line in lines:
            if line.startswith("•") or line.startswith("-"):
                html_lines.append(f"<li style='margin:4px 0'>{line[1:].strip()}</li>")
            elif line.strip() == "":
                html_lines.append("<br>")
            else:
                html_lines.append(f"<p style='margin:6px 0'>{line}</p>")

        html = f"""
        <html><body style="font-family:Arial,sans-serif;font-size:14px;
        color:#1a1a1a;max-width:600px;margin:0 auto;padding:24px;">
        <div style="border-left:3px solid #22c55e;padding-left:16px;margin-bottom:24px;">
          <p style="margin:0;font-size:12px;color:#6b7280;font-weight:600;
          text-transform:uppercase;letter-spacing:0.05em;">LeadQualifier AI</p>
        </div>
        {"".join(html_lines)}
        <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0">
        <p style="font-size:11px;color:#9ca3af">
          Diese E-Mail wurde automatisch generiert von LeadQualifier AI – GhostVenumAI
        </p>
        </body></html>
        """
        return html

    async def send_email(
        self,
        to: str,
        subject: str,
        body: str,
    ) -> bool:
        """
        Sendet eine E-Mail via Gmail SMTP.

        Args:
            to: Empfaenger-Adresse.
            subject: Betreff.
            body: E-Mail-Text (Plain-Text).

        Returns:
            True bei Erfolg, False bei Fehler.
        """
        if not self.sender_email or not self._app_password:
            logger.warning("GmailClient: Keine Credentials konfiguriert.")
            return False

        try:
            msg = self._build_message(to, subject, body)

            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.login(self.sender_email, self._app_password)
                server.sendmail(self.sender_email, to, msg.as_string())

            logger.info("GmailClient: E-Mail erfolgreich gesendet.")
            return True

        except smtplib.SMTPAuthenticationError:
            logger.error("GmailClient: Authentifizierung fehlgeschlagen. App-Passwort pruefen.")
            return False
        except smtplib.SMTPException as exc:
            logger.error("GmailClient: SMTP-Fehler: %s", type(exc).__name__)
            return False
        except Exception as exc:
            logger.error("GmailClient: Unerwarteter Fehler: %s", type(exc).__name__)
            return False

    async def send_lead_email(self, blackboard: LeadBlackboard) -> bool:
        """
        Sendet die fertige Lead-E-Mail aus dem Blackboard.

        Liest subject und body aus blackboard.final_email.
        Kein Logging von E-Mail-Adresse oder Inhalt (DSGVO).

        Args:
            blackboard: Befuelltes LeadBlackboard nach Reviewer-Freigabe.

        Returns:
            True bei Erfolg.
        """
        email_text = blackboard.final_email or blackboard.draft_email
        if not email_text:
            logger.warning("GmailClient: Kein E-Mail-Text im Blackboard.")
            return False

        # Betreff aus erster Zeile extrahieren (falls "Betreff: ..." Format)
        lines = email_text.strip().split("\n")
        subject = "Ihre Anfrage – LeadQualifier AI"
        body = email_text

        for i, line in enumerate(lines):
            if line.lower().startswith("betreff:"):
                subject = line.split(":", 1)[1].strip()
                body = "\n".join(lines[i + 1:]).strip()
                break

        return await self.send_email(
            to=blackboard.email,
            subject=subject,
            body=body,
        )
