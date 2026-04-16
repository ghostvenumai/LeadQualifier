"""
CRMClient: Webhook-basierte CRM-Integration.

Sendet qualifizierte Lead-Daten an ein externes CRM-System (z.B. HubSpot,
Pipedrive, Salesforce) via HTTP POST Webhook. Das Payload ist DSGVO-konform:
keine direkten E-Mail-Adressen oder Namen werden uebertragen.

Integration:
    Das CRM-System empfaengt die Lead-Daten und kann sie dort weiterverarbeiten.
    Bei Fehlern wird False zurueckgegeben ohne die Pipeline zu unterbrechen.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from core.blackboard import LeadBlackboard

logger = logging.getLogger(__name__)

# Erlaubte HTTP-Success-Status-Codes
_SUCCESS_STATUS_CODES: frozenset[int] = frozenset({200, 201, 202, 204})


class CRMClient:
    """
    Sendet Lead-Daten per Webhook an ein externes CRM-System.

    Alle Requests werden mit einem kurzen Timeout (5 Sekunden) gesendet,
    damit die Pipeline nicht blockiert wird. Bei Fehlern wird geloggt und
    False zurueckgegeben.

    Attributes:
        _webhook_url: URL des CRM-Webhooks.
    """

    def __init__(self, webhook_url: str) -> None:
        """
        Initialisiert den CRMClient.

        Args:
            webhook_url: Vollstaendige URL des CRM-Webhook-Endpoints.

        Raises:
            ValueError: Wenn die URL leer oder kein HTTP/HTTPS-Schema hat.
        """
        if not webhook_url or not webhook_url.startswith(("http://", "https://")):
            raise ValueError(
                "webhook_url muss eine gueltige HTTP/HTTPS-URL sein."
            )
        self._webhook_url: str = webhook_url

    async def push_qualified_lead(self, blackboard: LeadBlackboard) -> bool:
        """
        Sendet qualifizierte Lead-Daten an das CRM-System.

        Der Payload enthaelt nur nicht-sensible Felder:
        lead_id, company, score, email_type, industry, size, timestamp.
        E-Mail-Adresse und vollstaendiger Name werden NICHT uebertragen (DSGVO).

        Args:
            blackboard: Fertig verarbeitetes Blackboard mit Pipeline-Ergebnissen.

        Returns:
            True wenn das CRM den Payload erfolgreich empfangen hat (2xx).
        """
        try:
            payload = self._build_crm_payload(blackboard)

            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                response = await client.post(
                    self._webhook_url,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "LeadQualifier/1.0",
                        "X-Source": "leadqualifier-ai",
                    },
                )

            if response.status_code in _SUCCESS_STATUS_CODES:
                logger.info(
                    "CRMClient: Lead erfolgreich ans CRM uebertragen "
                    "fuer lead_id=%s (Status %d).",
                    blackboard.lead_id,
                    response.status_code,
                )
                blackboard.log(
                    "CRMClient",
                    f"crm_push_success: status={response.status_code}",
                )
                return True
            else:
                logger.warning(
                    "CRMClient: CRM antwortete mit Status %d fuer lead_id=%s.",
                    response.status_code,
                    blackboard.lead_id,
                )
                blackboard.log(
                    "CRMClient",
                    f"crm_push_failed: status={response.status_code}",
                )
                return False

        except httpx.TimeoutException:
            logger.warning(
                "CRMClient: Timeout beim CRM-Push fuer lead_id=%s.",
                blackboard.lead_id,
            )
            blackboard.log("CRMClient", "crm_push_timeout")
            return False
        except httpx.RequestError as exc:
            logger.warning(
                "CRMClient: Netzwerkfehler beim CRM-Push fuer lead_id=%s: %s",
                blackboard.lead_id,
                type(exc).__name__,
            )
            blackboard.log("CRMClient", f"crm_push_network_error: {type(exc).__name__}")
            return False
        except Exception as exc:
            logger.error(
                "CRMClient: Unerwarteter Fehler beim CRM-Push fuer lead_id=%s: %s",
                blackboard.lead_id,
                type(exc).__name__,
                exc_info=True,
            )
            blackboard.log("CRMClient", f"crm_push_error: {type(exc).__name__}")
            return False

    def _build_crm_payload(self, blackboard: LeadBlackboard) -> dict:
        """
        Erstellt einen DSGVO-konformen CRM-Payload aus dem Blackboard.

        Enthalt ausschliesslich nicht-sensible Felder. Kein name, keine
        E-Mail-Adresse. Das CRM kann die lead_id verwenden um die Daten
        intern mit dem eigenen Lead-Record zu verknuepfen.

        Args:
            blackboard: Blackboard mit allen Pipeline-Ergebnissen.

        Returns:
            CRM-Payload als Dictionary.
        """
        return {
            "lead_id": blackboard.lead_id,
            "company": blackboard.company,
            "score": blackboard.score,
            "email_type": blackboard.email_type,
            "review_passed": blackboard.review_passed,
            "industry": blackboard.research.get("industry", "unknown"),
            "size": blackboard.research.get("size", "unknown"),
            "website_quality": blackboard.research.get("website_quality", "unknown"),
            "is_decision_maker": blackboard.research.get("is_decision_maker", False),
            "score_details": blackboard.score_details,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "leadqualifier_ai",
            "version": "1.0",
        }

    async def test_connection(self) -> bool:
        """
        Prueft die Verbindung zum CRM-Webhook mit einem HEAD-Request.

        Nuetzlich beim Startup um sicherzustellen dass der Webhook erreichbar ist.

        Returns:
            True wenn der CRM-Endpoint erreichbar ist.
        """
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                response = await client.head(self._webhook_url)
            # HEAD-Requests koennen auch 405 zurueckgeben (Method Not Allowed)
            # Das bedeutet der Server ist erreichbar
            reachable = response.status_code < 500
            if reachable:
                logger.info(
                    "CRMClient: Verbindungstest erfolgreich (Status %d).",
                    response.status_code,
                )
            else:
                logger.warning(
                    "CRMClient: Verbindungstest fehlgeschlagen (Status %d).",
                    response.status_code,
                )
            return reachable

        except Exception as exc:
            logger.warning(
                "CRMClient: Verbindungstest fehlgeschlagen: %s",
                type(exc).__name__,
            )
            return False
