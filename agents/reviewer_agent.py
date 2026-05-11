"""
ReviewerAgent: Qualitaetskontrolle fuer E-Mail-Entwuerfe mit max. 2 Retries.

Prueft den Writer-Entwurf auf:
    - Professionalitaet und korrekte Sprache (Deutsch)
    - Keine Grammatikfehler
    - Personalisierung (enthaelt Firmenname)
    - Laenge (max. 300 Woerter)
    - Korrekte Sprache (Deutsch)

Bei Fehler ruft der ReviewerAgent den WriterAgent erneut auf (max. 2 Versuche).
Bei Freigabe: blackboard.review_passed = True, blackboard.final_email = blackboard.draft_email

DSGVO-Hinweis:
    Der E-Mail-Inhalt wird NICHT geloggt.
    Nur Metadaten (review_passed, attempt) kommen ins Log.
"""

import json
import logging

import anthropic

from core.blackboard import LeadBlackboard
from core.config import Settings

logger = logging.getLogger(__name__)

_MAX_RETRIES: int = 2
_MAX_WORDS: int = 300

import core.prompt_manager as _pm

_REVIEW_PROMPT_TEMPLATE = """\
Pruefe die folgende E-Mail auf Qualitaet fuer den B2B-Kontext.

## Zu pruefende E-Mail

**Firmenname des Empfaengers:** {company}
**E-Mail-Typ:** {email_type}
**Vollstaendiger E-Mail-Inhalt:**

---
{email_content}
---

## Pruefkriterien

Bewerte die E-Mail nach diesen Kriterien mit true/false:

1. **is_german**: Ist die E-Mail vollstaendig auf Deutsch geschrieben?
2. **is_professional**: Ist der Ton professionell und angemessen fuer B2B?
3. **no_grammar_errors**: Sind keine groben Grammatik- oder Rechtschreibfehler vorhanden?
4. **contains_company_name**: Ist der Firmenname "{company}" mindestens einmal enthalten?
5. **not_too_long**: Hat der E-Mail-Body maximal {max_words} Woerter?
6. **has_clear_cta**: Gibt es einen klaren naechsten Schritt oder Call-to-Action?
7. **no_placeholders**: Sind keine unfertigen Platzhalter wie [NAME], {{company}} etc. vorhanden?

## Entscheidungslogik

- passed: true NUR wenn ALLE 7 Kriterien true sind
- feedback: Konkrete Verbesserungshinweise auf Deutsch falls passed=false

## Pflicht-Ausgabeformat (NUR JSON)

{{
  "passed": <true oder false>,
  "criteria": {{
    "is_german": <true/false>,
    "is_professional": <true/false>,
    "no_grammar_errors": <true/false>,
    "contains_company_name": <true/false>,
    "not_too_long": <true/false>,
    "has_clear_cta": <true/false>,
    "no_placeholders": <true/false>
  }},
  "feedback": "<Konkretes Feedback auf Deutsch - leer wenn passed=true>",
  "word_count": <geschaetzte Wortanzahl als Integer>
}}
"""


class ReviewerAgent:
    """
    Prueft E-Mail-Entwuerfe auf Qualitaet und gibt bei Fehler Retries in Auftrag.

    Verwendet Claude Sonnet 4.6 fuer die Qualitaetspruefung.
    Bei nicht bestandenem Review wird der WriterAgent erneut aufgerufen.
    Maximal 2 Retry-Versuche (insgesamt 3 Write-Versuche moeglich).

    Bei bestandenem Review:
        - blackboard.review_passed = True
        - blackboard.final_email = blackboard.draft_email
        - blackboard.review_feedback = ""

    Bei endgueltigem Scheitern nach 2 Retries:
        - blackboard.review_passed = False
        - blackboard.final_email = blackboard.draft_email (letzter Versuch)
        - blackboard.review_feedback = letztes Feedback
    """

    def __init__(
        self,
        blackboard: LeadBlackboard,
        settings: Settings,
        writer_agent: "WriterAgent",  # type: ignore[name-defined]  # noqa: F821
    ) -> None:
        """
        Initialisiert den ReviewerAgent.

        Args:
            blackboard: Das gemeinsame Blackboard-Objekt der aktuellen Pipeline.
            settings: Konfigurationsobjekt mit API-Keys und Modell-Namen.
            writer_agent: Instanz des WriterAgent fuer Retry-Aufrufe.
        """
        self._blackboard = blackboard
        self._settings = settings
        self._writer_agent = writer_agent
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    async def review(self) -> bool:
        """
        Fuehrt die Qualitaetspruefung des aktuellen E-Mail-Entwurfs durch.

        Bei nicht bestandenem Review wird der WriterAgent erneut aufgerufen
        (max. 2 Retries). Nach dem letzten Retry wird der beste verfuegbare
        Entwurf als final_email gesetzt.

        Returns:
            True wenn die E-Mail die Qualitaetspruefung bestanden hat.
        """
        logger.info(
            "ReviewerAgent gestartet fuer lead_id=%s",
            self._blackboard.lead_id,
        )

        for attempt in range(1, _MAX_RETRIES + 2):  # 1, 2, 3
            is_first_attempt = attempt == 1

            if not is_first_attempt:
                # Retry: neuen Entwurf generieren
                logger.info(
                    "ReviewerAgent: Retry %d fuer lead_id=%s",
                    attempt - 1,
                    self._blackboard.lead_id,
                )
                try:
                    await self._writer_agent.write()
                except Exception as exc:
                    logger.error(
                        "ReviewerAgent: WriterAgent Retry %d fehlgeschlagen fuer lead_id=%s: %s",
                        attempt - 1,
                        self._blackboard.lead_id,
                        type(exc).__name__,
                    )
                    break

            # Review durchfuehren
            passed, feedback = await self._run_review(attempt)

            if passed:
                self._blackboard.review_passed = True
                self._blackboard.review_feedback = ""
                self._blackboard.final_email = self._blackboard.draft_email
                self._blackboard.log(
                    "ReviewerAgent",
                    f"review_passed: attempt={attempt}",
                )
                logger.info(
                    "ReviewerAgent: Review bestanden fuer lead_id=%s (Versuch %d)",
                    self._blackboard.lead_id,
                    attempt,
                )
                return True

            # Review fehlgeschlagen
            self._blackboard.review_feedback = feedback
            self._blackboard.log(
                "ReviewerAgent",
                f"review_failed: attempt={attempt}",
            )
            logger.warning(
                "ReviewerAgent: Review Versuch %d fehlgeschlagen fuer lead_id=%s",
                attempt,
                self._blackboard.lead_id,
            )

            if attempt >= _MAX_RETRIES + 1:
                # Letzter Versuch gescheitert - aktuellen Entwurf als final setzen
                logger.error(
                    "ReviewerAgent: Alle %d Versuche erschoepft fuer lead_id=%s - "
                    "Entwurf wird trotzdem als final gesetzt",
                    _MAX_RETRIES + 1,
                    self._blackboard.lead_id,
                )
                self._blackboard.review_passed = False
                self._blackboard.final_email = self._blackboard.draft_email
                return False

        # Fallback: Review nicht moeglich
        self._blackboard.review_passed = False
        self._blackboard.final_email = self._blackboard.draft_email
        self._blackboard.log("ReviewerAgent", "review_aborted: writer_retry_failed")
        return False

    async def _run_review(self, attempt: int) -> tuple[bool, str]:
        """
        Fuehrt eine einzelne Review-Runde durch.

        Args:
            attempt: Aktuelle Versuchsnummer (fuer Logging).

        Returns:
            Tuple (passed: bool, feedback: str).
        """
        bb = self._blackboard
        email_content = bb.draft_email

        if not email_content or len(email_content.strip()) < 50:
            logger.warning(
                "ReviewerAgent: Leerer Entwurf fuer lead_id=%s (Versuch %d)",
                bb.lead_id,
                attempt,
            )
            return False, "E-Mail-Entwurf ist leer oder zu kurz."

        # Wortanzahl vorab pruefen (ohne API-Call)
        word_count = self._count_words(email_content)
        if word_count > _MAX_WORDS * 1.5:
            # Deutlich zu lang - direkt abweisen ohne API-Aufruf
            return (
                False,
                f"E-Mail ist zu lang ({word_count} Woerter, Maximum: {_MAX_WORDS}).",
            )

        try:
            prompt = _REVIEW_PROMPT_TEMPLATE.format(
                company=bb.company,
                email_type=bb.email_type,
                email_content=email_content,
                max_words=_MAX_WORDS,
            )

            response = self._client.messages.create(
                model=self._settings.claude_agent_model,
                max_tokens=600,
                system=_pm.get("reviewer_system"),
                messages=[{"role": "user", "content": prompt}],
            )

            bb.track_cost(
                "ReviewerAgent",
                self._settings.claude_agent_model,
                response.usage.input_tokens,
                response.usage.output_tokens,
            )

            raw_text = response.content[0].text
            parsed = self._parse_response(raw_text)

            passed: bool = bool(parsed.get("passed", False))
            feedback: str = str(parsed.get("feedback", "")).strip()

            # Zusaetzlicher lokaler Check: Firmenname enthalten?
            if passed and bb.company.lower() not in email_content.lower():
                passed = False
                feedback = (
                    f"Firmenname '{bb.company}' ist nicht im E-Mail-Text enthalten."
                )

            return passed, feedback

        except anthropic.APIError as exc:
            logger.error(
                "ReviewerAgent: Claude API Fehler bei Versuch %d fuer lead_id=%s: %s",
                attempt,
                bb.lead_id,
                type(exc).__name__,
            )
            # Bei API-Fehler: konservativ als nicht bestanden werten
            return False, f"Review konnte nicht durchgefuehrt werden: {type(exc).__name__}"

        except (ValueError, KeyError, TypeError) as exc:
            logger.error(
                "ReviewerAgent: Parsing-Fehler bei Versuch %d fuer lead_id=%s: %s",
                attempt,
                bb.lead_id,
                exc,
            )
            return False, f"Review-Parsing fehlgeschlagen: {exc}"

    def _parse_response(self, raw_text: str) -> dict[str, object]:
        """
        Parst die Claude-Antwort als JSON.

        Args:
            raw_text: Rohtext der Claude API Antwort.

        Returns:
            Gepartes Dictionary.

        Raises:
            ValueError: Bei ungueltigem JSON.
        """
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

    def _count_words(self, text: str) -> int:
        """
        Zaehlt die Woerter in einem Text.

        Args:
            text: Zu zaehlendes Text-Fragment.

        Returns:
            Anzahl der Woerter (getrennt durch Leerzeichen).
        """
        return len(text.split())


# Typ-Annotation fuer den WriterAgent (vermeidet zirkulaere Imports)
from agents.writer_agent import WriterAgent  # noqa: E402 (after class definition)

ReviewerAgent.__init__.__annotations__["writer_agent"] = WriterAgent
