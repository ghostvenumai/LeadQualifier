"""
SlackClient: AES-256-GCM verschluesselte Slack-Benachrichtigungen.

Sendet formatierte Alerts an einen Slack-Incoming-Webhook. Sensible Felder
werden vor dem Versand mit AES-256-GCM verschluesselt. Der Payload ist
DSGVO-konform: keine E-Mail-Adressen, keine Klarnamen.

Verschluesselungsformat:
    Base64-encoded "nonce:ciphertext" wobei nonce 12 Bytes (96-bit) ist.
    Der Empfaenger muss den gleichen 32-Byte-Key zur Entschluesselung besitzen.
"""

import base64
import json
import logging
import os
from typing import Optional

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.blackboard import LeadBlackboard

logger = logging.getLogger(__name__)


class SlackClient:
    """
    Sendet DSGVO-konforme und optionale verschluesselte Slack-Alerts.

    Verwendet Slack Block Kit fuer formatierte Nachrichten und AES-256-GCM
    fuer die Verschluesselung sensibler Felder. Die E-Mail-Adresse des Leads
    wird niemals in Slack-Nachrichten uebertragen.

    Attributes:
        _webhook_url: Slack Incoming Webhook URL.
        _encryption_key: 32-Byte AES-256 Schluessel fuer Verschluesselung.
    """

    def __init__(self, webhook_url: str, encryption_key: bytes) -> None:
        """
        Initialisiert den SlackClient.

        Args:
            webhook_url: Slack Incoming Webhook URL.
            encryption_key: Exakt 32 Bytes fuer AES-256-GCM.

        Raises:
            ValueError: Wenn encryption_key nicht genau 32 Bytes lang ist.
        """
        if len(encryption_key) != 32:
            raise ValueError(
                f"encryption_key muss genau 32 Bytes sein, "
                f"erhalten: {len(encryption_key)} Bytes."
            )
        self._webhook_url: str = webhook_url
        self._encryption_key: bytes = encryption_key

    def encrypt_message(self, plaintext: str) -> str:
        """
        Verschluesselt einen Klartext-String mit AES-256-GCM.

        Generiert einen kryptographisch sicheren 12-Byte-Nonce (96-bit)
        und verschluesselt den Text. Das Ergebnis wird als Base64-codierter
        String im Format "nonce:ciphertext" zurueckgegeben.

        Args:
            plaintext: Zu verschluesselnder Klartext.

        Returns:
            Base64-codierter String im Format "nonce_b64:ciphertext_b64".

        Raises:
            RuntimeError: Wenn die Verschluesselung fehlschlaegt.
        """
        try:
            nonce: bytes = os.urandom(12)  # 96-bit Nonce fuer GCM
            aesgcm = AESGCM(self._encryption_key)
            ciphertext: bytes = aesgcm.encrypt(
                nonce,
                plaintext.encode("utf-8"),
                None,  # Kein AAD (additional authenticated data)
            )
            nonce_b64 = base64.b64encode(nonce).decode("ascii")
            ciphertext_b64 = base64.b64encode(ciphertext).decode("ascii")
            return f"{nonce_b64}:{ciphertext_b64}"
        except Exception as exc:
            logger.error(
                "SlackClient: Verschluesselung fehlgeschlagen: %s",
                type(exc).__name__,
                exc_info=True,
            )
            raise RuntimeError(f"Verschluesselung fehlgeschlagen: {type(exc).__name__}") from exc

    async def send_qualified_lead_alert(self, blackboard: LeadBlackboard) -> bool:
        """
        Sendet einen formatierten Slack-Alert fuer einen qualifizierten Lead.

        Erstellt einen Block-Kit-Payload mit gruenen Attachment (qualifiziert).
        Sensible Felder (score_reasoning) werden vor dem Versand verschluesselt.
        E-Mail-Adresse und vollstaendiger Name werden NICHT uebertragen (DSGVO).

        Args:
            blackboard: Fertig verarbeitetes Blackboard mit allen Ergebnissen.

        Returns:
            True wenn der Alert erfolgreich gesendet wurde.
        """
        try:
            # Sensible Felder verschluesseln
            encrypted_reasoning = self.encrypt_message(blackboard.score_reasoning or "")

            payload = self._build_qualified_payload(
                lead_id=blackboard.lead_id,
                company=blackboard.company,
                score=blackboard.score,
                encrypted_reasoning=encrypted_reasoning,
                email_type=blackboard.email_type,
                industry=blackboard.research.get("industry", "unbekannt"),
                size=blackboard.research.get("size", "unbekannt"),
            )

            return await self._send_payload(payload)

        except Exception as exc:
            logger.error(
                "SlackClient: send_qualified_lead_alert fehlgeschlagen "
                "fuer lead_id=%s: %s",
                blackboard.lead_id,
                type(exc).__name__,
                exc_info=True,
            )
            return False

    async def send_error_alert(self, error: str, lead_id: str) -> bool:
        """
        Sendet einen Fehler-Alert an Slack.

        Wird aufgerufen wenn die Pipeline fuer einen Lead fehlschlaegt.
        Enthalt keine personenbezogenen Daten, nur die Lead-ID und den
        Fehlertyp.

        Args:
            error: Fehlermeldung (keine PII erlaubt).
            lead_id: UUID des betroffenen Leads.

        Returns:
            True wenn der Alert erfolgreich gesendet wurde.
        """
        try:
            payload = {
                "text": f":x: Pipeline-Fehler fuer Lead `{lead_id}`",
                "attachments": [
                    {
                        "color": "#FF0000",
                        "blocks": [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": (
                                        f":x: *Pipeline-Fehler*\n"
                                        f"*Lead-ID:* `{lead_id}`\n"
                                        f"*Fehler:* `{error[:200]}`"
                                    ),
                                },
                            },
                            {
                                "type": "context",
                                "elements": [
                                    {
                                        "type": "mrkdwn",
                                        "text": "LeadQualifier AI | Bitte Logs pruefen.",
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
            return await self._send_payload(payload)

        except Exception as exc:
            logger.error(
                "SlackClient: send_error_alert fehlgeschlagen fuer lead_id=%s: %s",
                lead_id,
                type(exc).__name__,
                exc_info=True,
            )
            return False

    def _build_qualified_payload(
        self,
        lead_id: str,
        company: str,
        score: int,
        encrypted_reasoning: str,
        email_type: str,
        industry: str,
        size: str,
    ) -> dict:
        """
        Baut den Slack Block Kit Payload fuer einen qualifizierten Lead.

        Verwendet gruene Farbe (#36A64F) fuer den Attachment-Rahmen.
        Das verschluesselte Reasoning wird als Code-Block eingebettet.

        Args:
            lead_id: Eindeutige Lead-ID (UUID).
            company: Firmenname (kein vollstaendiger Klarname).
            score: Lead-Score (1-10).
            encrypted_reasoning: AES-256-GCM verschluesseltes Reasoning.
            email_type: Typ der versendeten E-Mail.
            industry: Erkannte Branche.
            size: Erkannte Unternehmensgroesse.

        Returns:
            Slack-Payload als Dictionary.
        """
        score_bar = ":green_circle:" * score + ":white_circle:" * (10 - score)

        return {
            "text": f":white_check_mark: Qualifizierter Lead: {company} (Score: {score}/10)",
            "attachments": [
                {
                    "color": "#36A64F",  # Gruen fuer qualifiziert
                    "blocks": [
                        {
                            "type": "header",
                            "text": {
                                "type": "plain_text",
                                "text": ":white_check_mark: Neuer qualifizierter Lead",
                                "emoji": True,
                            },
                        },
                        {
                            "type": "section",
                            "fields": [
                                {
                                    "type": "mrkdwn",
                                    "text": f"*Unternehmen:*\n{company}",
                                },
                                {
                                    "type": "mrkdwn",
                                    "text": f"*Score:*\n{score}/10",
                                },
                                {
                                    "type": "mrkdwn",
                                    "text": f"*Branche:*\n{industry}",
                                },
                                {
                                    "type": "mrkdwn",
                                    "text": f"*Groesse:*\n{size}",
                                },
                                {
                                    "type": "mrkdwn",
                                    "text": f"*E-Mail-Typ:*\n{email_type}",
                                },
                                {
                                    "type": "mrkdwn",
                                    "text": f"*Lead-ID:*\n`{lead_id}`",
                                },
                            ],
                        },
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"*Score-Verlauf:*\n{score_bar}",
                            },
                        },
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f"*Reasoning (verschluesselt):*\n"
                                    f"```{encrypted_reasoning[:100]}...```"
                                ),
                            },
                        },
                        {
                            "type": "divider",
                        },
                        {
                            "type": "context",
                            "elements": [
                                {
                                    "type": "mrkdwn",
                                    "text": (
                                        "LeadQualifier AI | "
                                        "Reasoning ist AES-256-GCM verschluesselt | "
                                        "DSGVO-konform"
                                    ),
                                }
                            ],
                        },
                    ],
                }
            ],
        }

    async def _send_payload(self, payload: dict) -> bool:
        """
        Sendet einen JSON-Payload an den konfigurierten Slack Webhook.

        Args:
            payload: Vollstaendiger Slack-Payload als Dictionary.

        Returns:
            True bei HTTP 200, sonst False.
        """
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                response = await client.post(
                    self._webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )

            if response.status_code == 200:
                logger.debug("SlackClient: Payload erfolgreich gesendet.")
                return True
            else:
                logger.warning(
                    "SlackClient: Slack-Webhook antwortete mit Status %d.",
                    response.status_code,
                )
                return False

        except httpx.TimeoutException:
            logger.warning("SlackClient: Timeout beim Senden des Payloads.")
            return False
        except httpx.RequestError as exc:
            logger.warning(
                "SlackClient: Netzwerkfehler beim Senden: %s",
                type(exc).__name__,
            )
            return False
        except Exception as exc:
            logger.error(
                "SlackClient: Unerwarteter Fehler beim Senden: %s",
                type(exc).__name__,
                exc_info=True,
            )
            return False
