"""
LeadBlackboard: Gemeinsames Gedaechtnis (Shared Memory) fuer alle Agents.

Jeder Agent liest und schreibt auf dieses Blackboard. Die Pipeline-Reihenfolge:
Orchestrator -> Research + Scoring (parallel) -> Writer -> Reviewer -> Output
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class LeadBlackboard:
    """
    Zentrales Shared-Memory-Objekt fuer die Lead-Qualifizierungs-Pipeline.

    Wird beim Eingang eines neuen Leads erstellt und durch alle fuenf Agents
    befuellt. Jeder Agent liest vorherige Ergebnisse und schreibt seine
    eigenen Ergebnisse in die dafuer vorgesehenen Felder.

    Attributes:
        lead_id: Eindeutige UUID des Leads.
        name: Vollstaendiger Name des Lead-Kontakts.
        email: E-Mail-Adresse des Leads (wird NICHT geloggt).
        company: Firmenname des Leads.
        message: Originalnachricht des Leads aus dem Kontaktformular.
        research: Vom Research Agent befuelltes Dictionary mit Firmendaten.
        score: Vom Scoring Agent berechneter Score (1-10).
        score_reasoning: Begruendung des Scores als Freitext.
        score_details: Detaillierte Aufschluesslung der 12 Scoring-Kriterien.
        draft_email: Vom Writer Agent erstellter E-Mail-Entwurf.
        email_type: Typ der E-Mail ("qualify" oder "reject").
        review_passed: Ob der Reviewer den Entwurf akzeptiert hat.
        review_feedback: Feedback des Reviewers zum Entwurf.
        final_email: Finale, versandbereite E-Mail nach dem Review.
        agent_log: Chronologisches Audit-Log aller Agent-Aktionen.
        created_at: Zeitstempel der Lead-Erstellung.
    """

    # INPUT (vom Webhook befuellt)
    lead_id: str
    name: str
    email: str
    company: str
    message: str

    # RESEARCH AGENT fuellt das
    research: dict = field(default_factory=dict)
    # Erwartete Struktur:
    # {
    #   "industry": "IT",
    #   "size": "50-200",
    #   "is_decision_maker": True,
    #   "website_quality": "professional",
    #   "linkedin_url": "https://linkedin.com/in/...",
    #   "website_url": "https://...",
    #   "news_items": [...],
    #   "cached": False
    # }

    # SCORING AGENT fuellt das
    score: int = 0
    score_reasoning: str = ""
    score_details: dict = field(default_factory=dict)
    # score_details enthaelt die 12 Kriterien:
    # {
    #   "decision_maker": 1, "company_size": 1, "industry_fit": 1,
    #   "budget_signals": 1, "urgency": 1, "message_quality": 1,
    #   "website_quality": 1, "linkedin_presence": 1, "news_activity": 1,
    #   "tech_stack_fit": 1, "geo_relevance": 1, "contact_completeness": 1
    # }

    # WRITER AGENT fuellt das
    draft_email: str = ""
    email_type: str = ""  # "qualify" oder "reject"

    # REVIEWER AGENT fuellt das
    review_passed: bool = False
    review_feedback: str = ""
    final_email: str = ""

    # AUDIT LOG
    agent_log: list = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)

    def log(self, agent: str, action: str) -> None:
        """
        Fuegt einen Eintrag in den Agent-Audit-Log ein.

        Wichtig: E-Mail-Adressen und andere personenbezogene Daten duerfen
        NICHT in action-Strings uebergeben werden (DSGVO).

        Args:
            agent: Name des Agents, der die Aktion ausfuehrt.
            action: Beschreibung der Aktion (keine PII).
        """
        self.agent_log.append({
            "agent": agent,
            "action": action,
            "timestamp": datetime.now().isoformat()
        })

    def to_summary(self) -> dict:
        """
        Gibt eine DSGVO-konforme Zusammenfassung des Blackboards zurueck.

        E-Mail-Adresse wird nicht zurueckgegeben. Geeignet fuer Logging
        und Monitoring ohne personenbezogene Daten.

        Returns:
            Dictionary mit nicht-sensiblen Feldern des Blackboards.
        """
        return {
            "lead_id": self.lead_id,
            "company": self.company,
            "score": self.score,
            "email_type": self.email_type,
            "review_passed": self.review_passed,
            "agent_log_count": len(self.agent_log),
            "created_at": self.created_at.isoformat()
        }

    def is_complete(self) -> bool:
        """
        Prueft ob die Pipeline fuer diesen Lead vollstaendig abgeschlossen ist.

        Returns:
            True wenn alle Agents ihre Arbeit abgeschlossen haben.
        """
        return (
            bool(self.research)
            and self.score > 0
            and bool(self.draft_email)
            and bool(self.final_email)
        )
