"""
WriterAgent: Erstellt personalisierte E-Mails basierend auf Lead-Score und Research-Daten.

Verwendet Claude Opus 4.6 (hoechste Qualitaet) fuer die E-Mail-Erstellung.

Logik:
    - Score >= QUALIFY_THRESHOLD (default 7): Qualifizierungs-Mail (professionell, personalisiert)
    - Score < QUALIFY_THRESHOLD: Absage-Mail (respektvoll, kurz, auf Deutsch)

DSGVO-Hinweis:
    Der E-Mail-Inhalt wird NICHT geloggt. Nur der Typ (qualify/reject) wird ins Log geschrieben.
"""

import logging
from typing import Literal

import anthropic

from core.blackboard import LeadBlackboard
from core.config import Settings
from core.models import EmailDraft

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_QUALIFY = """\
Du bist ein erfahrener B2B-Vertriebsprofi bei GhostVenumAI im DACH-Markt.
Du schreibst professionelle, personalisierte Qualifizierungs-E-Mails auf Deutsch.

Deine E-Mails sind:
- Professionell und auf Augenhöhe mit dem Empfaenger
- Konkret und auf die spezifische Situation des Unternehmens eingegangen
- Kurz und praegnant (max. 250 Woerter im Body)
- Klar mit einem Call-to-Action (z.B. Termin, Demo, Gespraech)
- Kein generisches "Schablonen-Gefuehl"

Du antwortest AUSSCHLIESSLICH mit einem JSON-Objekt, kein Freitext davor oder danach.
"""

_SYSTEM_PROMPT_REJECT = """\
Du bist ein erfahrener B2B-Vertriebsprofi bei GhostVenumAI im DACH-Markt.
Du schreibst professionelle, respektvolle Absage-E-Mails auf Deutsch.

Deine Absage-E-Mails sind:
- Respektvoll und wertschaetzend
- Kurz (max. 120 Woerter im Body)
- Ohne Begruendung (kein "du bist zu klein/klein" etc.)
- Lassen die Tuer fuer die Zukunft offen
- Professionell und menschlich

Du antwortest AUSSCHLIESSLICH mit einem JSON-Objekt, kein Freitext davor oder danach.
"""

_QUALIFY_PROMPT_TEMPLATE = """\
Erstelle eine personalisierte Qualifizierungs-E-Mail fuer den folgenden Lead.

## Lead-Informationen

**Kontaktperson:** {contact_name}
**Unternehmen:** {company}
**Branche:** {industry}
**Unternehmensgroesse:** {size}
**Score:** {score}/10
**Nachricht des Leads:** {message}
**Firmenlocation:** {location}
**Aktuelle News zum Unternehmen:** {news_items}
**Website:** {website_url}

## Aufgabe

Schreibe eine professionelle Qualifizierungs-E-Mail auf Deutsch.
- Beginne NICHT mit "Sehr geehrte/r" - nutze "Hallo {contact_first_name},"
- Gehe konkret auf die Anfrage ein
- Erwaehne das Unternehmen "{company}" mindestens einmal
- Schlage einen konkreten naechsten Schritt vor (z.B. 30-Minuten-Discovery-Call)
- Unterschreibe als "Dein GhostVenumAI Team"
- Maximal 250 Woerter im Body

## Pflicht-Ausgabeformat (NUR JSON)

{{
  "subject": "<Betreffzeile - konkret und personalisiert, max 80 Zeichen>",
  "body": "<Vollstaendiger E-Mail-Body auf Deutsch>"
}}
"""

_REJECT_PROMPT_TEMPLATE = """\
Erstelle eine professionelle Absage-E-Mail fuer den folgenden Lead.

## Lead-Informationen

**Kontaktperson:** {contact_name}
**Unternehmen:** {company}
**Nachricht des Leads:** {message}

## Aufgabe

Schreibe eine kurze, respektvolle Absage-E-Mail auf Deutsch.
- Beginne NICHT mit "Sehr geehrte/r" - nutze "Hallo {contact_first_name},"
- Bedanke dich fuer die Anfrage
- Sage hoeflich ab ohne genaue Begruendung
- Erwaehne das Unternehmen "{company}"
- Lasse die Tuer fuer spaetere Zusammenarbeit offen
- Unterschreibe als "Dein GhostVenumAI Team"
- Maximal 120 Woerter im Body

## Pflicht-Ausgabeformat (NUR JSON)

{{
  "subject": "<Betreffzeile - neutral und respektvoll, max 80 Zeichen>",
  "body": "<Vollstaendiger Absage-E-Mail-Body auf Deutsch>"
}}
"""


class WriterAgent:
    """
    Erstellt personalisierte Qualifizierungs- oder Absage-E-Mails.

    Liest Score, Research-Daten und Lead-Informationen aus dem Blackboard
    und erstellt via Claude Opus 4.6 eine passende E-Mail.
    Das Ergebnis wird in blackboard.draft_email und blackboard.email_type
    gespeichert.
    """

    def __init__(self, blackboard: LeadBlackboard, settings: Settings) -> None:
        """
        Initialisiert den WriterAgent.

        Args:
            blackboard: Das gemeinsame Blackboard-Objekt der aktuellen Pipeline.
            settings: Konfigurationsobjekt mit API-Keys und Modell-Namen.
        """
        self._blackboard = blackboard
        self._settings = settings
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    async def write(self) -> EmailDraft:
        """
        Erstellt die E-Mail basierend auf Score und Research-Daten.

        Bestimmt anhand des Scores den E-Mail-Typ (qualify/reject) und
        generiert via Claude Opus einen personalisierten Entwurf.
        Schreibt das Ergebnis in blackboard.draft_email und blackboard.email_type.

        Returns:
            EmailDraft mit subject, body, email_type und tone.

        Raises:
            Keine Exception - bei Fehlern wird ein Fallback-Draft zurueckgegeben.
        """
        bb = self._blackboard
        threshold = self._settings.qualify_threshold

        email_type: Literal["qualify", "reject"] = (
            "qualify" if bb.score >= threshold else "reject"
        )

        logger.info(
            "WriterAgent gestartet: lead_id=%s, email_type=%s, score=%d",
            bb.lead_id,
            email_type,
            bb.score,
        )

        try:
            draft = await self._generate_email(email_type)

            bb.draft_email = f"Betreff: {draft.subject}\n\n{draft.body}"
            bb.email_type = email_type

            bb.log("WriterAgent", f"email_drafted: type={email_type}")
            logger.info(
                "WriterAgent abgeschlossen: lead_id=%s, email_type=%s",
                bb.lead_id,
                email_type,
            )

            return draft

        except anthropic.APIError as exc:
            logger.error(
                "WriterAgent: Claude API Fehler fuer lead_id=%s: %s",
                bb.lead_id,
                type(exc).__name__,
            )
            return self._fallback_draft(email_type)

        except (ValueError, KeyError, TypeError) as exc:
            logger.error(
                "WriterAgent: Parsing-Fehler fuer lead_id=%s: %s",
                bb.lead_id,
                exc,
            )
            return self._fallback_draft(email_type)

    async def _generate_email(
        self, email_type: Literal["qualify", "reject"]
    ) -> EmailDraft:
        """
        Ruft Claude Opus auf und generiert den E-Mail-Entwurf.

        Args:
            email_type: "qualify" oder "reject".

        Returns:
            Geparster EmailDraft.

        Raises:
            anthropic.APIError: Bei API-Fehlern.
            ValueError: Bei ungueltigem JSON in der Antwort.
        """
        bb = self._blackboard
        research = bb.research

        contact_first_name = bb.name.split()[0] if bb.name.split() else bb.name

        if email_type == "qualify":
            system_prompt = _SYSTEM_PROMPT_QUALIFY
            news_summary = (
                ", ".join(research.get("news_items", [])[:2])
                if research.get("news_items")
                else "Keine aktuellen News verfuegbar"
            )
            user_prompt = _QUALIFY_PROMPT_TEMPLATE.format(
                contact_name=bb.name,
                contact_first_name=contact_first_name,
                company=bb.company,
                industry=research.get("industry", "unbekannt"),
                size=research.get("size", "unbekannt"),
                score=bb.score,
                message=bb.message,
                location=research.get("location") or "nicht ermittelt",
                news_items=news_summary,
                website_url=research.get("website_url") or "nicht verfuegbar",
            )
        else:
            system_prompt = _SYSTEM_PROMPT_REJECT
            user_prompt = _REJECT_PROMPT_TEMPLATE.format(
                contact_name=bb.name,
                contact_first_name=contact_first_name,
                company=bb.company,
                message=bb.message,
            )

        response = self._client.messages.create(
            model=self._settings.claude_writer_model,
            max_tokens=1000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        self._blackboard.track_cost(
            "WriterAgent",
            self._settings.claude_writer_model,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )

        raw_text = response.content[0].text
        parsed = self._parse_response(raw_text)

        subject = parsed.get("subject", "").strip()
        body = parsed.get("body", "").strip()

        if not subject or not body:
            raise ValueError("Claude-Antwort enthaelt kein subject oder body.")

        return EmailDraft(
            subject=subject,
            body=body,
            email_type=email_type,
            tone="professional",
        )

    def _parse_response(self, raw_text: str) -> dict[str, str]:
        """
        Parst die Claude-Antwort als JSON.

        Bereinigt Markdown-Codeblock-Wrapper falls vorhanden.

        Args:
            raw_text: Rohtext aus der Claude API Antwort.

        Returns:
            Dictionary mit "subject" und "body".

        Raises:
            ValueError: Bei ungueltigem JSON.
        """
        import json

        text = raw_text.strip()

        # Markdown-Codeblock entfernen
        if text.startswith("```"):
            lines = text.split("\n")
            end_idx = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
            text = "\n".join(lines[1:end_idx]).strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Antwort ist kein valides JSON: {exc}") from exc

    def _fallback_draft(self, email_type: Literal["qualify", "reject"]) -> EmailDraft:
        """
        Erstellt einen einfachen Fallback-Draft bei API-Fehlern.

        Schreibt den Fallback in das Blackboard damit die Pipeline
        weiterlaufen kann.

        Args:
            email_type: "qualify" oder "reject".

        Returns:
            Einfacher EmailDraft mit generischem Inhalt.
        """
        bb = self._blackboard
        contact_first_name = bb.name.split()[0] if bb.name.split() else bb.name

        if email_type == "qualify":
            subject = f"Ihre Anfrage bei GhostVenumAI - {bb.company}"
            body = (
                f"Hallo {contact_first_name},\n\n"
                f"vielen Dank fuer Ihre Anfrage. Wir haben Ihre Nachricht erhalten "
                f"und werden uns zeitnah bei Ihnen melden, um die Details zu besprechen.\n\n"
                f"Gerne wuerden wir in einem kurzen Discovery-Call mehr ueber die "
                f"Anforderungen bei {bb.company} erfahren.\n\n"
                f"Mit freundlichen Gruessen\n"
                f"Dein GhostVenumAI Team"
            )
        else:
            subject = f"Re: Ihre Anfrage - {bb.company}"
            body = (
                f"Hallo {contact_first_name},\n\n"
                f"vielen Dank fuer Ihre Anfrage und Ihr Interesse an GhostVenumAI.\n\n"
                f"Nach sorgfaeltiger Pruefung muessen wir Ihnen leider mitteilen, "
                f"dass wir zum jetzigen Zeitpunkt keine passende Zusammenarbeit "
                f"realisieren koennen.\n\n"
                f"Wir wuenschen Ihnen und {bb.company} weiterhin viel Erfolg.\n\n"
                f"Mit freundlichen Gruessen\n"
                f"Dein GhostVenumAI Team"
            )

        draft = EmailDraft(
            subject=subject,
            body=body,
            email_type=email_type,
            tone="professional",
        )

        bb.draft_email = f"Betreff: {draft.subject}\n\n{draft.body}"
        bb.email_type = email_type
        bb.log("WriterAgent", f"email_drafted_fallback: type={email_type}")

        return draft
