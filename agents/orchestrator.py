"""
OrchestratorAgent: Steuert die gesamte Lead-Qualifizierungs-Pipeline.

Ablauf:
    1. LeadBlackboard erstellen (UUID als lead_id)
    2. Research + Scoring PARALLEL starten (asyncio.gather)
    3. WriterAgent ausfuehren
    4. ReviewerAgent ausfuehren
    5. Je nach Score: Slack-Alert + CRM-Webhook senden
    6. PipelineResult zurueckgeben (inkl. Laufzeit)

Bei jedem nicht abfangbaren Fehler wird ein PipelineResult mit error-Feld
zurueckgegeben - die Pipeline crasht den Webhook nie.

DSGVO-Hinweis:
    Keine E-Mail-Adressen oder Namen in Log-Ausgaben.
    Nur lead_id, score und email_type werden geloggt.
"""

import asyncio
import logging
import time
import uuid
from typing import Optional

import httpx

from core.blackboard import LeadBlackboard
from core.config import Settings
from core.memory import MemoryDB
from core.models import LeadInput, PipelineResult

from agents.research_agent import ResearchAgent
from agents.scoring_agent import ScoringAgent
from agents.writer_agent import WriterAgent
from agents.reviewer_agent import ReviewerAgent

logger = logging.getLogger(__name__)


class OrchestratorAgent:
    """
    Koordiniert alle fuenf Agents der Lead-Qualifizierungs-Pipeline.

    Erstellt das LeadBlackboard, startet Research und Scoring parallel
    via asyncio.gather(), fuehrt anschliessend Writer und Reviewer aus
    und loest Slack/CRM-Notifications aus.

    Der Orchestrator ist der einzige Einstiegspunkt fuer die Pipeline.
    Er wird direkt vom Flask-Webhook aufgerufen.
    """

    def __init__(self, settings: Settings, db: Optional[MemoryDB] = None) -> None:
        """
        Initialisiert den OrchestratorAgent.

        Args:
            settings: Konfigurationsobjekt mit allen Umgebungsvariablen.
            db: Optionale MemoryDB-Instanz fuer Lead-Persistenz.
        """
        self._settings = settings
        self._db = db

    async def run(self, lead_input: LeadInput) -> PipelineResult:
        """
        Fuehrt die vollstaendige Lead-Qualifizierungs-Pipeline aus.

        Erstellt ein LeadBlackboard, startet alle Agents in der korrekten
        Reihenfolge (Research + Scoring parallel, dann Writer, dann Reviewer)
        und gibt ein PipelineResult mit allen relevanten Ergebnissen zurueck.

        Args:
            lead_input: Validiertes LeadInput-Objekt aus dem Webhook.

        Returns:
            PipelineResult mit score, email_type, duration_seconds und
            optionalem error-Feld.
        """
        lead_id = str(uuid.uuid4())
        start_time = time.monotonic()

        blackboard = LeadBlackboard(
            lead_id=lead_id,
            name=lead_input.name,
            email=lead_input.email,
            company=lead_input.company,
            message=lead_input.message,
        )

        blackboard.log("OrchestratorAgent", "pipeline_started")
        logger.info("OrchestratorAgent: Pipeline gestartet fuer lead_id=%s", lead_id)

        try:
            # --- Phase 1: Research + Scoring PARALLEL ---
            blackboard.log("OrchestratorAgent", "phase1_parallel_start")

            research_agent = ResearchAgent(blackboard=blackboard, settings=self._settings)
            scoring_agent = ScoringAgent(blackboard=blackboard, settings=self._settings)

            research_result, score_result = await asyncio.gather(
                research_agent.research(),
                scoring_agent.score(),
                return_exceptions=False,
            )

            blackboard.log(
                "OrchestratorAgent",
                f"phase1_complete: score={blackboard.score}",
            )
            logger.info(
                "OrchestratorAgent: Phase 1 abgeschlossen fuer lead_id=%s, score=%d",
                lead_id,
                blackboard.score,
            )

            # --- Phase 2: Writer ---
            blackboard.log("OrchestratorAgent", "phase2_writer_start")

            writer_agent = WriterAgent(blackboard=blackboard, settings=self._settings)
            await writer_agent.write()

            blackboard.log(
                "OrchestratorAgent",
                f"phase2_complete: email_type={blackboard.email_type}",
            )

            # --- Phase 3: Reviewer ---
            blackboard.log("OrchestratorAgent", "phase3_reviewer_start")

            reviewer_agent = ReviewerAgent(
                blackboard=blackboard,
                settings=self._settings,
                writer_agent=writer_agent,
            )
            review_passed = await reviewer_agent.review()

            blackboard.log(
                "OrchestratorAgent",
                f"phase3_complete: review_passed={review_passed}",
            )
            logger.info(
                "OrchestratorAgent: Phase 3 abgeschlossen fuer lead_id=%s, review_passed=%s",
                lead_id,
                review_passed,
            )

            # --- Phase 4: E-Mail + Notifications ---
            slack_notified = False
            email_sent = False

            # E-Mail immer senden (qualify UND reject)
            email_sent = await self._send_email(blackboard)

            if blackboard.score >= self._settings.qualify_threshold:
                blackboard.log("OrchestratorAgent", "notifications_start: qualified_lead")
                slack_notified = await self._send_slack_alert(blackboard)
                await self._send_crm_webhook(blackboard)
            else:
                blackboard.log("OrchestratorAgent", "notifications_skipped: low_score")

            duration = time.monotonic() - start_time

            blackboard.log(
                "OrchestratorAgent",
                f"pipeline_complete: duration={duration:.2f}s",
            )
            logger.info(
                "OrchestratorAgent: Pipeline abgeschlossen fuer lead_id=%s in %.2fs",
                lead_id,
                duration,
            )

            if self._db:
                self._db.save_lead(blackboard)

            return PipelineResult(
                lead_id=lead_id,
                score=blackboard.score,
                email_type=blackboard.email_type,
                email_sent=email_sent,
                slack_notified=slack_notified,
                duration_seconds=round(duration, 3),
                cost_usd=round(blackboard.api_cost_usd, 6),
                error=None,
            )

        except Exception as exc:
            duration = time.monotonic() - start_time
            error_type = type(exc).__name__

            logger.error(
                "OrchestratorAgent: Pipeline fehlgeschlagen fuer lead_id=%s "
                "nach %.2fs: %s",
                lead_id,
                duration,
                error_type,
                exc_info=True,
            )

            blackboard.log(
                "OrchestratorAgent",
                f"pipeline_error: {error_type} after {duration:.2f}s",
            )

            return PipelineResult(
                lead_id=lead_id,
                score=blackboard.score,
                email_type=blackboard.email_type,
                email_sent=False,
                slack_notified=False,
                duration_seconds=round(duration, 3),
                error=f"Pipeline-Fehler: {error_type}",
            )

    async def _send_email(self, blackboard: LeadBlackboard) -> bool:
        """
        Sendet die generierte E-Mail via Gmail SMTP.

        Args:
            blackboard: Blackboard mit final_email und email-Adresse.

        Returns:
            True wenn E-Mail erfolgreich gesendet.
        """
        sender = self._settings.gmail_sender_email
        password = getattr(self._settings, 'gmail_app_password', None)

        if not sender or not password:
            logger.debug("OrchestratorAgent: Gmail nicht konfiguriert, E-Mail uebersprungen.")
            return False

        try:
            from integrations.gmail_client import GmailClient
            client = GmailClient(sender_email=sender, app_password=password)
            override = getattr(self._settings, "email_override", None)
            sent = await client.send_lead_email(blackboard, override_to=override)
            if sent:
                blackboard.log("OrchestratorAgent", f"email_sent: type={blackboard.email_type}")
            return sent
        except Exception as exc:
            logger.error("OrchestratorAgent: E-Mail-Fehler: %s", type(exc).__name__)
            return False

    async def _send_slack_alert(self, blackboard: LeadBlackboard) -> bool:
        """
        Sendet einen Slack-Alert fuer qualifizierte Leads.

        Verwendet den konfigurierten Slack Webhook URL. Falls kein URL
        konfiguriert ist, wird der Alert uebersprungen.
        Slack-Payload ist DSGVO-konform (keine E-Mail-Adresse).

        Args:
            blackboard: Blackboard mit allen Pipeline-Ergebnissen.

        Returns:
            True wenn der Alert erfolgreich gesendet wurde.
        """
        webhook_url = self._settings.slack_webhook_url
        if not webhook_url:
            logger.debug(
                "OrchestratorAgent: Slack nicht konfiguriert, Alert uebersprungen "
                "fuer lead_id=%s",
                blackboard.lead_id,
            )
            return False

        try:
            # DSGVO-konformes Payload (keine E-Mail, kein vollstaendiger Name)
            payload = self._build_slack_payload(blackboard)

            # AES-256-GCM Verschluesselung falls konfiguriert
            encryption_key = self._settings.slack_encryption_key
            if encryption_key:
                payload = self._encrypt_slack_payload(payload, encryption_key)

            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                response = await client.post(webhook_url, json=payload)

            if response.status_code == 200:
                blackboard.log("OrchestratorAgent", "slack_alert_sent")
                logger.info(
                    "OrchestratorAgent: Slack-Alert gesendet fuer lead_id=%s",
                    blackboard.lead_id,
                )
                return True
            else:
                logger.warning(
                    "OrchestratorAgent: Slack-Alert fehlgeschlagen (Status %d) "
                    "fuer lead_id=%s",
                    response.status_code,
                    blackboard.lead_id,
                )
                return False

        except httpx.RequestError as exc:
            logger.warning(
                "OrchestratorAgent: Slack-Request fehlgeschlagen fuer lead_id=%s: %s",
                blackboard.lead_id,
                type(exc).__name__,
            )
            return False

        except Exception as exc:
            logger.error(
                "OrchestratorAgent: Unerwarteter Slack-Fehler fuer lead_id=%s: %s",
                blackboard.lead_id,
                type(exc).__name__,
            )
            return False

    def _build_slack_payload(self, blackboard: LeadBlackboard) -> dict[str, object]:
        """
        Baut ein DSGVO-konformes Slack-Payload auf.

        Enthaelt keine E-Mail-Adresse oder vollstaendigen Klarnamen.

        Args:
            blackboard: Blackboard mit Pipeline-Ergebnissen.

        Returns:
            Slack-Payload als Dictionary.
        """
        score_emoji = ":star:" if blackboard.score >= 9 else ":white_check_mark:"
        industry = blackboard.research.get("industry", "unbekannt")
        size = blackboard.research.get("size", "unbekannt")

        return {
            "text": (
                f"{score_emoji} *Neuer qualifizierter Lead!*\n"
                f"*Lead-ID:* `{blackboard.lead_id}`\n"
                f"*Unternehmen:* {blackboard.company}\n"
                f"*Score:* {blackboard.score}/10\n"
                f"*Branche:* {industry}\n"
                f"*Groesse:* {size} Mitarbeiter\n"
                f"*E-Mail-Typ:* {blackboard.email_type}"
            ),
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"{score_emoji} *Neuer qualifizierter Lead!*\n"
                            f"*Unternehmen:* {blackboard.company} | "
                            f"*Score:* {blackboard.score}/10 | "
                            f"*Branche:* {industry}"
                        ),
                    },
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"Lead-ID: `{blackboard.lead_id}` | "
                                    f"Groesse: {size} MA",
                        }
                    ],
                },
            ],
        }

    def _encrypt_slack_payload(
        self,
        payload: dict[str, object],
        key: bytes,
    ) -> dict[str, str]:
        """
        Verschluesselt den Slack-Payload mit AES-256-GCM.

        Args:
            payload: Unverschluesseltes Payload-Dictionary.
            key: 32-Byte AES-256 Schluessel.

        Returns:
            Dictionary mit verschluesseltem Payload als Base64-String.

        Raises:
            ImportError: Wenn die cryptography-Library nicht installiert ist.
        """
        import base64
        import json as json_module
        import os

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        plaintext = json_module.dumps(payload, ensure_ascii=False).encode("utf-8")
        nonce = os.urandom(12)  # 96-bit Nonce fuer GCM
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)

        return {
            "encrypted": base64.b64encode(nonce + ciphertext).decode("ascii"),
            "version": "aes256gcm-v1",
        }

    async def _send_crm_webhook(self, blackboard: LeadBlackboard) -> bool:
        """
        Sendet Lead-Daten an das externe CRM-System.

        Falls kein CRM-Webhook konfiguriert ist, wird der Schritt uebersprungen.
        Payload enthaelt keine vollstaendige E-Mail-Adresse (DSGVO-Minimalprinzip).

        Args:
            blackboard: Blackboard mit Pipeline-Ergebnissen.

        Returns:
            True wenn der CRM-Webhook erfolgreich gesendet wurde.
        """
        crm_url = self._settings.crm_webhook_url
        if not crm_url:
            logger.debug(
                "OrchestratorAgent: CRM nicht konfiguriert, Webhook uebersprungen "
                "fuer lead_id=%s",
                blackboard.lead_id,
            )
            return False

        try:
            crm_payload = {
                "lead_id": blackboard.lead_id,
                "company": blackboard.company,
                "score": blackboard.score,
                "email_type": blackboard.email_type,
                "industry": blackboard.research.get("industry", "unknown"),
                "size": blackboard.research.get("size", "unknown"),
                "review_passed": blackboard.review_passed,
                "source": "leadqualifier_ai",
            }

            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                response = await client.post(crm_url, json=crm_payload)

            if response.status_code in (200, 201, 202):
                blackboard.log("OrchestratorAgent", "crm_webhook_sent")
                logger.info(
                    "OrchestratorAgent: CRM-Webhook gesendet fuer lead_id=%s",
                    blackboard.lead_id,
                )
                return True
            else:
                logger.warning(
                    "OrchestratorAgent: CRM-Webhook fehlgeschlagen (Status %d) "
                    "fuer lead_id=%s",
                    response.status_code,
                    blackboard.lead_id,
                )
                return False

        except httpx.RequestError as exc:
            logger.warning(
                "OrchestratorAgent: CRM-Request fehlgeschlagen fuer lead_id=%s: %s",
                blackboard.lead_id,
                type(exc).__name__,
            )
            return False

        except Exception as exc:
            logger.error(
                "OrchestratorAgent: Unerwarteter CRM-Fehler fuer lead_id=%s: %s",
                blackboard.lead_id,
                type(exc).__name__,
            )
            return False
