"""
ScoringAgent: Bewertet einen Lead nach 12 definierten Kriterien.

Ruft die Claude Sonnet API mit strukturiertem JSON-Output auf und schreibt
den berechneten Score (1-10) samt Detailbewertung in das LeadBlackboard.

Kriterien und Punktgewichtung:
  +3  Entscheider (CEO/CTO/GF/Inhaber)
  +2  Firmengrösse passt (10-500 MA)
  +2  Branche relevant (IT/Marketing/Beratung)
  +1  Nachricht konkret - Budget oder Zeitrahmen erwaehnt
  +1  Professionelle Firmenwebsite vorhanden
  +1  LinkedIn-Profil vollstaendig
  +1  Konkreter Zeitrahmen genannt (z.B. Q2/Q3)
  +1  Professionelle E-Mail-Domain (kein Gmail/GMX/Web.de etc.)
  +1  Positive Vorinteraktion (Lead-History vorhanden)
  +1  Aktuelle News: Wachstum oder Investition
  -3  Praktikant oder Student erkannt
  -1  Vage Nachricht ohne konkreten Bedarf
"""

import json
import logging
from typing import Any, Optional, TYPE_CHECKING

import anthropic

from core.blackboard import LeadBlackboard
from core.config import Settings
from core.models import ScoreResult

if TYPE_CHECKING:
    from core.memory import MemoryDB

logger = logging.getLogger(__name__)

# Maximale Rohpunktzahl (Summe aller positiven Kriterien)
_MAX_RAW_SCORE: int = 14  # 3+2+2+1+1+1+1+1+1+1 = 14

# Erzwungener Tool-Use-Output: garantiert valides JSON statt Freitext-Parsing
_CRITERIA_KEYS: list[str] = [
    "decision_maker", "company_size", "industry_fit", "budget_signals",
    "urgency", "message_quality", "website_quality", "linkedin_presence",
    "news_activity", "tech_stack_fit", "geo_relevance", "contact_completeness",
    "is_student_intern", "vague_message",
]

_SCORE_TOOL: dict = {
    "name": "submit_score",
    "description": "Uebermittelt das strukturierte Bewertungs-Ergebnis fuer den Lead.",
    "input_schema": {
        "type": "object",
        "properties": {
            "criteria": {
                "type": "object",
                "properties": {key: {"type": "integer"} for key in _CRITERIA_KEYS},
                "required": _CRITERIA_KEYS,
                "additionalProperties": False,
            },
            "raw_points": {"type": "integer"},
            "score": {"type": "integer"},
            "reasoning": {"type": "string"},
        },
        "required": ["criteria", "raw_points", "score", "reasoning"],
        "additionalProperties": False,
    },
}

import core.prompt_manager as _pm

_USER_PROMPT_TEMPLATE = """\
Bewerte den folgenden B2B-Lead anhand der 12 Kriterien und gib ein strukturiertes JSON-Ergebnis zurueck.

## Lead-Daten

**Firmenname:** {company}
**Kontaktperson:** {contact_name}
**Nachricht:** {message}

## Recherchierte Firmendaten

**Branche:** {industry}
**Unternehmensgrösse:** {size}
**Ist Entscheidungstraeger:** {is_decision_maker}
**Website-Qualitaet:** {website_quality}
**LinkedIn-URL vorhanden:** {has_linkedin}
**Aktuelle News:** {news_items}
**Professionelle E-Mail-Domain:** {has_professional_email}
**Unternehmen sucht aktuell Mitarbeiter:** {is_hiring}
**Anzahl offener Stellen:** {open_positions_count}

---

## Bewertungsregeln (Pflicht)

Bewerte jedes der 12 Kriterien mit dem angegebenen Punktwert (0 = nicht erfuellt, voller Wert = erfuellt):

| Kriterium-Key         | Punkte wenn erfuellt | Beschreibung                                      |
|-----------------------|----------------------|---------------------------------------------------|
| decision_maker        | +3                   | Kontakt ist CEO, CTO, GF, Inhaber oder VP         |
| company_size          | +2                   | Firmengrösse liegt zwischen 10 und 500 Mitarbeitern |
| industry_fit          | +2                   | Branche ist IT, Marketing, Beratung oder SaaS     |
| budget_signals        | +1                   | Nachricht erwaehnt Budget, Investition oder Kosten — ODER Unternehmen sucht aktuell Mitarbeiter (Wachstumssignal) |
| urgency               | +1                   | Konkreter Zeitrahmen genannt (Q2, Q3, dieses Jahr etc.) |
| message_quality       | +1                   | Nachricht ist konkret, spezifisch, kein generisches Interesse |
| website_quality       | +1                   | Professionelle Unternehmenswebsite vorhanden      |
| linkedin_presence     | +1                   | LinkedIn-URL vorhanden und vollstaendig           |
| news_activity         | +1                   | Aktuelle News zu Wachstum, Investition oder Expansion |
| tech_stack_fit        | +1                   | Unternehmen nutzt relevante Technologien (Cloud, SaaS, CRM) |
| geo_relevance         | +1                   | Unternehmen ist im DACH-Raum (D/A/CH) ansaessig  |
| contact_completeness  | +1                   | Alle Kontaktdaten vollstaendig (Name, Firma, E-Mail) |

**ABZUEGE (wenn erkannt):**
| Kriterium-Key     | Punkte | Beschreibung                                    |
|-------------------|--------|-------------------------------------------------|
| is_student_intern | -3     | Kontakt ist Student, Praktikant oder Werkstudent |
| vague_message     | -1     | Nachricht ist vage, generisch ohne konkreten Bedarf |

---

## Pflicht-Ausgabeformat (NUR JSON, kein Markdown-Codeblock)

{{
  "criteria": {{
    "decision_maker": <0 oder 3>,
    "company_size": <0 oder 2>,
    "industry_fit": <0 oder 2>,
    "budget_signals": <0 oder 1>,
    "urgency": <0 oder 1>,
    "message_quality": <0 oder 1>,
    "website_quality": <0 oder 1>,
    "linkedin_presence": <0 oder 1>,
    "news_activity": <0 oder 1>,
    "tech_stack_fit": <0 oder 1>,
    "geo_relevance": <0 oder 1>,
    "contact_completeness": <0 oder 1>,
    "is_student_intern": <0 oder -3>,
    "vague_message": <0 oder -1>
  }},
  "raw_points": <Summe aller Einzelpunkte als Integer>,
  "score": <normalisierter Score 1-10 als Integer>,
  "reasoning": "<Ausfuehrliche Begruendung auf Deutsch, min. 50 Woerter>"
}}
"""


class ScoringAgent:
    """
    Bewertet einen Lead nach 12 Kriterien und schreibt das Ergebnis in das Blackboard.

    Verwendet die Claude Sonnet API mit erzwungenem JSON-Output.
    Das Ergebnis wird in blackboard.score, blackboard.score_details
    und blackboard.score_reasoning gespeichert.
    """

    def __init__(
        self,
        blackboard: LeadBlackboard,
        settings: Settings,
        db: Optional["MemoryDB"] = None,
    ) -> None:
        """
        Initialisiert den ScoringAgent.

        Args:
            blackboard: Das gemeinsame Blackboard-Objekt der aktuellen Pipeline.
            settings: Konfigurationsobjekt mit API-Keys und Modell-Namen.
            db: Optionale MemoryDB-Instanz. Wenn gesetzt, werden Kalibrierungs-
                Hinweise aus dem Scoring-Feedback in den Prompt injiziert.
        """
        self._blackboard = blackboard
        self._settings = settings
        self._db = db
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            max_retries=settings.anthropic_max_retries,
            timeout=settings.anthropic_timeout_seconds,
        )

    def _build_user_prompt(self) -> str:
        """
        Erstellt den User-Prompt aus den aktuellen Blackboard-Daten.

        Kombiniert Lead-Eingabedaten mit den Research-Ergebnissen zu einem
        vollstaendigen Bewertungskontext fuer Claude.

        Returns:
            Formatierter Prompt-String fuer die Claude API.
        """
        research = self._blackboard.research
        bb = self._blackboard

        # E-Mail-Domain-Pruefung (kein Logging der E-Mail selbst)
        email_domain = ""
        if bb.email and "@" in bb.email:
            email_domain = bb.email.split("@")[-1].lower()
        free_domains = {
            "gmail.com", "gmx.de", "gmx.net", "gmx.at", "web.de",
            "hotmail.com", "hotmail.de", "yahoo.com", "yahoo.de",
            "outlook.com", "t-online.de", "freenet.de", "icloud.com",
        }
        has_professional_email = email_domain not in free_domains and bool(email_domain)

        news_items = research.get("news_items", [])
        news_summary = ", ".join(news_items[:3]) if news_items else "Keine aktuellen News gefunden"

        # Hiring-Status fuer Prompt aufbereiten
        is_hiring_raw = research.get("is_hiring")
        open_positions = research.get("open_positions_count", 0)
        if is_hiring_raw is True:
            is_hiring_label = f"Ja ({open_positions} offene Stellen gefunden — Wachstumssignal)"
        elif is_hiring_raw is False:
            is_hiring_label = "Nein (keine aktiven Stellenanzeigen gefunden)"
        else:
            is_hiring_label = "Unbekannt (nicht ermittelbar)"

        prompt = _USER_PROMPT_TEMPLATE.format(
            company=bb.company,
            contact_name=bb.name,
            message=bb.message,
            industry=research.get("industry", "unbekannt"),
            size=research.get("size", "unbekannt"),
            is_decision_maker=research.get("is_decision_maker", False),
            website_quality=research.get("website_quality", "unknown"),
            has_linkedin=bool(research.get("linkedin_url")),
            news_items=news_summary,
            has_professional_email=has_professional_email,
            is_hiring=is_hiring_label,
            open_positions_count=open_positions,
        )

        calibration_hint = self._build_calibration_hint()
        if calibration_hint:
            prompt += "\n" + calibration_hint

        return prompt

    def _build_calibration_hint(self) -> str:
        """
        Erstellt Kalibrierungs-Hinweise aus dem gesammelten Scoring-Feedback.

        Liest aggregierte Statistiken (vorhergesagter Score vs. tatsaechliches
        Ergebnis) aus der MemoryDB und formatiert sie als Prompt-Abschnitt.
        Es fliessen nur numerische Aggregate ein - keine Lead-Daten (DSGVO,
        keine Prompt-Injection-Flaeche).

        Returns:
            Formatierter Hinweis-Block oder leerer String wenn keine DB
            vorhanden ist oder zu wenige Feedback-Datenpunkte existieren.
        """
        if self._db is None:
            return ""

        try:
            stats = self._db.get_scoring_calibration(
                qualify_threshold=self._settings.qualify_threshold,
            )
        except Exception as exc:
            logger.warning(
                "ScoringAgent: Kalibrierung nicht ladbar: %s", type(exc).__name__
            )
            return ""

        if not stats:
            return ""

        threshold = self._settings.qualify_threshold
        lines = [
            "## Kalibrierung aus realem Vertriebs-Feedback "
            f"(n={stats['sample_count']} bewertete Leads)",
        ]
        if stats["avg_score_converted"] is not None:
            lines.append(
                f"- Durchschnittlicher Score spaeter KONVERTIERTER Leads: "
                f"{stats['avg_score_converted']:.1f}"
            )
        if stats["avg_score_not_converted"] is not None:
            lines.append(
                f"- Durchschnittlicher Score NICHT konvertierter Leads: "
                f"{stats['avg_score_not_converted']:.1f}"
            )
        if stats["qualified_total"] > 0:
            lines.append(
                f"- {stats['false_positives']} von {stats['qualified_total']} Leads "
                f"mit Score >= {threshold} haben NICHT konvertiert"
                + (
                    " -> sei bei Grenzfaellen strenger"
                    if stats["false_positives"] * 2 > stats["qualified_total"]
                    else ""
                )
            )
        if stats["false_negatives"] > 0:
            lines.append(
                f"- {stats['false_negatives']} Lead(s) mit Score < {threshold} "
                f"haben trotzdem konvertiert -> pruefe Grenzfaelle wohlwollend"
            )
        lines.append(
            "Nutze diese Kalibrierung nur bei Grenzfaellen - "
            "die Punktwerte der Kriterien bleiben unveraendert."
        )
        return "\n".join(lines)

    def _parse_claude_response(self, raw_text: str) -> dict[str, Any]:
        """
        Parst die Claude-Antwort als JSON.

        Bereinigt Markdown-Codeblock-Wrapper falls Claude diese trotz
        Anweisung hinzufuegt.

        Args:
            raw_text: Rohtext aus der Claude API Antwort.

        Returns:
            Gepartes Dictionary mit score, criteria und reasoning.

        Raises:
            ValueError: Wenn kein valides JSON gefunden werden kann.
        """
        text = raw_text.strip()

        # Markdown-Codeblock entfernen falls vorhanden
        if text.startswith("```"):
            lines = text.split("\n")
            # Erste und letzte Zeile (``` bzw. ```json) entfernen
            text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
            text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Claude-Antwort ist kein valides JSON: {exc}") from exc

    def _extract_payload(self, response: Any) -> dict[str, Any]:
        """
        Extrahiert das Bewertungs-Payload aus der Claude-Antwort.

        Bevorzugt den Tool-Use-Block (garantiert schema-valides JSON durch
        tool_choice). Faellt auf Text-JSON-Parsing zurueck, falls kein
        Tool-Use-Block vorhanden ist.

        Args:
            response: Message-Objekt der Claude API.

        Returns:
            Geparstes Dictionary mit criteria, raw_points, score, reasoning.

        Raises:
            ValueError: Wenn weder Tool-Use- noch valides JSON gefunden wird.
        """
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                return dict(block.input)
        return self._parse_claude_response(response.content[0].text)

    def _normalize_score(self, raw_points: int) -> int:
        """
        Normalisiert Rohpunkte auf einen Score von 1-10.

        Berechnung: score = round((raw_points / MAX_RAW) * 10)
        Anschliessend auf den Bereich [0, 10] geclipt.

        Args:
            raw_points: Summe der Einzelpunkte aus allen Kriterien.

        Returns:
            Normalisierter Score zwischen 0 und 10 (int).
        """
        if _MAX_RAW_SCORE <= 0:
            return 1
        normalized = round((raw_points / _MAX_RAW_SCORE) * 10)
        return max(0, min(10, normalized))

    async def score(self) -> ScoreResult:
        """
        Bewertet den Lead und schreibt das Ergebnis in das Blackboard.

        Ruft die Claude Sonnet API auf, parst den strukturierten JSON-Output
        und speichert Score, Details und Begruendung im Blackboard.
        Bei Fehlern wird ein Minimal-Score (1) mit Fehler-Reasoning zurueckgegeben.

        Returns:
            ScoreResult mit score (1-10), reasoning und details.
        """
        logger.info(
            "ScoringAgent gestartet fuer lead_id=%s",
            self._blackboard.lead_id,
        )

        try:
            user_prompt = self._build_user_prompt()

            response = await self._client.messages.create(
                model=self._settings.claude_agent_model,
                max_tokens=1500,
                system=_pm.get("scoring_system"),
                messages=[
                    {"role": "user", "content": user_prompt}
                ],
                tools=[_SCORE_TOOL],
                tool_choice={"type": "tool", "name": "submit_score"},
            )

            self._blackboard.track_cost(
                "ScoringAgent",
                self._settings.claude_agent_model,
                response.usage.input_tokens,
                response.usage.output_tokens,
            )

            parsed = self._extract_payload(response)

            # Kriterien-Dict extrahieren und validieren
            criteria: dict[str, Any] = parsed.get("criteria", {})
            raw_points: int = int(parsed.get("raw_points", sum(criteria.values())))
            reasoning: str = parsed.get("reasoning", "Keine Begruendung erhalten.")

            # Score aus Claude-Antwort oder selbst berechnen
            claude_score: int = int(parsed.get("score", self._normalize_score(raw_points)))
            final_score: int = max(0, min(10, claude_score))

            # Details fuer ScoreResult (12 Hauptkriterien ohne Abzuege)
            main_criteria_keys = [
                "decision_maker", "company_size", "industry_fit",
                "budget_signals", "urgency", "message_quality",
                "website_quality", "linkedin_presence", "news_activity",
                "tech_stack_fit", "geo_relevance", "contact_completeness",
            ]
            score_details: dict[str, Any] = {
                k: criteria.get(k, 0) for k in main_criteria_keys
            }
            # Abzuege ebenfalls speichern
            score_details["is_student_intern"] = criteria.get("is_student_intern", 0)
            score_details["vague_message"] = criteria.get("vague_message", 0)
            score_details["raw_points"] = raw_points

            # Pydantic erfordert score >= 1 - bei 0 auf 1 setzen mit Hinweis
            pydantic_score = max(1, final_score)

            result = ScoreResult(
                score=pydantic_score,
                reasoning=reasoning,
                details=score_details,
            )

            # In Blackboard schreiben
            self._blackboard.score = final_score  # echten Wert inkl. 0 speichern
            self._blackboard.score_details = score_details
            self._blackboard.score_reasoning = reasoning

            self._blackboard.log(
                "ScoringAgent",
                f"scoring_complete: score={final_score}, raw_points={raw_points}",
            )
            logger.info(
                "ScoringAgent abgeschlossen: lead_id=%s, score=%d",
                self._blackboard.lead_id,
                final_score,
            )

            return result

        except anthropic.APIError as exc:
            logger.error(
                "ScoringAgent: Claude API Fehler fuer lead_id=%s: %s",
                self._blackboard.lead_id,
                type(exc).__name__,
            )
            return self._fallback_score(f"Claude API Fehler: {type(exc).__name__}")

        except (ValueError, KeyError, TypeError) as exc:
            logger.error(
                "ScoringAgent: Parsing-Fehler fuer lead_id=%s: %s",
                self._blackboard.lead_id,
                exc,
            )
            return self._fallback_score(f"Parsing-Fehler: {exc}")

    def _fallback_score(self, reason: str) -> ScoreResult:
        """
        Erstellt einen Fallback-ScoreResult bei nicht behebbaren Fehlern.

        Schreibt einen Score von 1 in das Blackboard damit die Pipeline
        weiterlaufen kann (Absage-Mail wird erstellt).

        Args:
            reason: Fehlerursache fuer das Reasoning-Feld.

        Returns:
            ScoreResult mit Minimal-Score 1.
        """
        fallback = ScoreResult(
            score=1,
            reasoning=f"Bewertung konnte nicht durchgefuehrt werden: {reason}",
            details={},
        )
        self._blackboard.score = 1
        self._blackboard.score_reasoning = fallback.reasoning
        self._blackboard.score_details = {}
        self._blackboard.log("ScoringAgent", f"scoring_failed: {reason}")
        return fallback
