"""
Pydantic v2 Datenmodelle fuer die LeadQualifier AI Pipeline.

Alle eingehenden und ausgehenden Datenstrukturen werden hier definiert
und validiert. Verwendet Pydantic v2 mit strikten Type Hints.
"""

from typing import Any, Literal, Optional
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from datetime import datetime


class LeadInput(BaseModel):
    """
    Eingehendes Lead-Datenmodell vom Webhook.

    Validiert und normalisiert alle Eingabedaten bevor sie in die
    Pipeline eingespeist werden.

    Attributes:
        name: Vollstaendiger Name des Kontakts.
        email: E-Mail-Adresse (wird validiert).
        company: Firmenname (Pflichtfeld).
        message: Nachricht aus dem Kontaktformular.
        source: Optionale Angabe woher der Lead kommt (z.B. "website", "referral").
    """

    model_config = {"from_attributes": True}

    name: str = Field(
        ...,
        min_length=2,
        max_length=200,
        description="Vollstaendiger Name des Lead-Kontakts"
    )
    email: EmailStr = Field(
        ...,
        description="Geschaeftliche E-Mail-Adresse des Leads"
    )
    company: str = Field(
        ...,
        min_length=1,
        max_length=300,
        description="Firmenname des Leads"
    )
    message: str = Field(
        ...,
        min_length=10,
        max_length=5000,
        description="Anfragenachricht aus dem Kontaktformular"
    )
    source: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Herkunftskanal des Leads (z.B. 'website', 'referral', 'linkedin')"
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Bereinigt den Namen von fuehrendem/nachfolgendem Whitespace."""
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Name darf nicht nur aus Leerzeichen bestehen.")
        return cleaned

    @field_validator("company")
    @classmethod
    def validate_company(cls, v: str) -> str:
        """Bereinigt den Firmennamen von fuehrendem/nachfolgendem Whitespace."""
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Firmenname darf nicht nur aus Leerzeichen bestehen.")
        return cleaned

    @field_validator("message")
    @classmethod
    def validate_message(cls, v: str) -> str:
        """Bereinigt die Nachricht und prueft auf Mindestgehalt."""
        cleaned = v.strip()
        if len(cleaned) < 10:
            raise ValueError("Nachricht muss mindestens 10 Zeichen enthalten.")
        return cleaned


class ResearchData(BaseModel):
    """
    Ergebnisdaten des Research Agents.

    Enthaelt alle gesammelten Informationen ueber das Unternehmen des Leads,
    die fuer den Scoring-Prozess benoetigt werden.

    Attributes:
        industry: Branche des Unternehmens (z.B. "IT", "Finance", "Healthcare").
        size: Mitarbeitergroesse als Range (z.B. "50-200", "1-10", "200+").
        is_decision_maker: Ob der Kontakt Entscheidungstraeger ist.
        website_quality: Bewertung der Unternehmenswebsite.
        linkedin_url: LinkedIn-Profillink des Kontakts (optional).
        website_url: URL der Unternehmenswebsite (optional).
        news_items: Liste aktueller Nachrichten ueber das Unternehmen.
        cached: Ob die Daten aus dem Cache stammen (nicht frisch recherchiert).
        tech_stack: Identifizierte Technologien des Unternehmens.
        location: Standort des Unternehmens (relevant fuer DACH-Qualifizierung).
        annual_revenue: Geschaetzter Jahresumsatz (optional, aus oeffentlichen Quellen).
    """

    model_config = {"from_attributes": True}

    industry: str = Field(
        default="unknown",
        max_length=100,
        description="Branche des Unternehmens"
    )
    size: str = Field(
        default="unknown",
        max_length=50,
        description="Mitarbeiteranzahl als Range (z.B. '50-200')"
    )
    is_decision_maker: bool = Field(
        default=False,
        description="Ob der Lead-Kontakt Entscheidungsberechtigter ist"
    )
    website_quality: Literal["professional", "average", "poor", "none", "unknown"] = Field(
        default="unknown",
        description="Qualitaetsbewertung der Unternehmenswebsite"
    )
    linkedin_url: Optional[str] = Field(
        default=None,
        max_length=500,
        description="LinkedIn-URL des Lead-Kontakts"
    )
    website_url: Optional[str] = Field(
        default=None,
        max_length=500,
        description="URL der Unternehmenswebsite"
    )
    news_items: list[str] = Field(
        default_factory=list,
        description="Aktuelle Nachrichten/Pressemeldungen zum Unternehmen"
    )
    cached: bool = Field(
        default=False,
        description="True wenn Daten aus dem DB-Cache stammen"
    )
    tech_stack: list[str] = Field(
        default_factory=list,
        description="Identifizierte Technologien des Unternehmens"
    )
    location: Optional[str] = Field(
        default=None,
        max_length=200,
        description="Unternehmensstandort (Stadt, Land)"
    )
    annual_revenue: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Geschaetzter Jahresumsatz aus oeffentlichen Quellen"
    )
    is_hiring: Optional[bool] = Field(
        default=None,
        description="True wenn aktuell offene Stellen gefunden wurden, False wenn nicht, None wenn unbekannt"
    )
    open_positions_count: int = Field(
        default=0,
        ge=0,
        description="Anzahl gefundener offener Stellen (Website + Indeed)"
    )
    job_page_url: Optional[str] = Field(
        default=None,
        max_length=500,
        description="URL der Karriere- oder Jobs-Seite des Unternehmens"
    )

    @field_validator("news_items")
    @classmethod
    def limit_news_items(cls, v: list[str]) -> list[str]:
        """Begrenzt die Anzahl der News-Items auf maximal 10."""
        return v[:10]


class ScoreResult(BaseModel):
    """
    Ergebnis des Scoring Agents.

    Enthaelt den berechneten Lead-Score, die Begruendung und die detaillierte
    Aufschluesslung aller 12 Scoring-Kriterien.

    Attributes:
        score: Gesamtscore von 1 (niedrig) bis 10 (hoch).
        reasoning: Ausfuehrliche Begruendung fuer den Score.
        details: Aufschluesslung der 12 Einzelkriterien (je 0 oder 1).
    """

    model_config = {"from_attributes": True}

    score: int = Field(
        ...,
        ge=1,
        le=10,
        description="Lead-Qualitaetsscore von 1 (schlecht) bis 10 (hervorragend)"
    )
    reasoning: str = Field(
        ...,
        min_length=20,
        max_length=2000,
        description="Ausfuehrliche Begruendung fuer den vergebenen Score"
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Detailbewertung der 12 Scoring-Kriterien"
    )

    @model_validator(mode="after")
    def validate_details_structure(self) -> "ScoreResult":
        """
        Stellt sicher dass alle 12 Scoring-Kriterien im details-Dict vorhanden sind.
        Fehlende Kriterien werden mit 0 aufgefuellt.
        """
        required_criteria = [
            "decision_maker",
            "company_size",
            "industry_fit",
            "budget_signals",
            "urgency",
            "message_quality",
            "website_quality",
            "linkedin_presence",
            "news_activity",
            "tech_stack_fit",
            "geo_relevance",
            "contact_completeness"
        ]
        for criterion in required_criteria:
            if criterion not in self.details:
                self.details[criterion] = 0
        return self


class EmailDraft(BaseModel):
    """
    E-Mail-Entwurf des Writer Agents.

    Repraesentiert den erstellten Qualifizierungs- oder Ablehnungs-E-Mail-Entwurf
    vor dem Review-Prozess.

    Attributes:
        subject: Betreffzeile der E-Mail.
        body: Vollstaendiger E-Mail-Text (plain text oder HTML).
        email_type: Typ der E-Mail.
        tone: Sprachlicher Ton des Entwurfs.
    """

    model_config = {"from_attributes": True}

    subject: str = Field(
        ...,
        min_length=5,
        max_length=200,
        description="Betreffzeile der E-Mail"
    )
    body: str = Field(
        ...,
        min_length=50,
        max_length=10000,
        description="Vollstaendiger E-Mail-Body"
    )
    email_type: Literal["qualify", "reject"] = Field(
        ...,
        description="Typ der E-Mail: 'qualify' fuer qualifizierte Leads, 'reject' fuer Absagen"
    )
    tone: str = Field(
        default="professional",
        max_length=50,
        description="Tonalitaet der E-Mail (z.B. 'professional', 'friendly', 'formal')"
    )

    @field_validator("subject")
    @classmethod
    def clean_subject(cls, v: str) -> str:
        """Entfernt Zeilenumbrueche aus der Betreffzeile."""
        return v.replace("\n", " ").replace("\r", " ").strip()


class LicenseInfo(BaseModel):
    """
    Lizenzstatus des LeadQualifier AI Systems.

    Enthaelt alle Informationen zur aktuellen Lizenz, Nutzungslimits
    und verbleibenden Ressourcen.

    Attributes:
        type: Lizenztyp (demo, starter, professional, enterprise).
        active: Ob die Lizenz aktuell aktiv ist.
        leads_remaining: Verbleibende Leads (nur fuer Demo und Starter).
        days_remaining: Verbleibende Tage (nur fuer Demo-Lizenz).
        reason: Optionale Erklaerung bei inaktiver Lizenz.
    """

    model_config = {"from_attributes": True}

    type: Literal["demo", "starter", "professional", "enterprise"] = Field(
        ...,
        description="Typ der Lizenz"
    )
    active: bool = Field(
        ...,
        description="True wenn die Lizenz derzeit gueltig und nutzbar ist"
    )
    leads_remaining: Optional[int] = Field(
        default=None,
        ge=0,
        description="Verbleibende Lead-Verarbeitungen (None = unlimitiert)"
    )
    days_remaining: Optional[int] = Field(
        default=None,
        ge=0,
        description="Verbleibende Tage der Demo-Lizenz (None = unlimitiert)"
    )
    reason: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Erklaerung bei inaktiver Lizenz oder Limit-Erreichen"
    )

    @model_validator(mode="after")
    def demo_requires_limits(self) -> "LicenseInfo":
        """Demo-Lizenzen muessen immer Limitangaben enthalten."""
        if self.type == "demo":
            if self.leads_remaining is None or self.days_remaining is None:
                raise ValueError(
                    "Demo-Lizenz muss leads_remaining und days_remaining enthalten."
                )
        return self


class PipelineResult(BaseModel):
    """
    Gesamtergebnis der Lead-Qualifizierungs-Pipeline.

    Wird am Ende der Pipeline erstellt und enthaelt alle relevanten
    Ergebnisinformationen fuer Logging, Monitoring und CRM-Integration.

    Attributes:
        lead_id: UUID des verarbeiteten Leads.
        score: Berechneter Lead-Score (1-10).
        email_type: Typ der versendeten/erstellten E-Mail.
        email_sent: Ob die E-Mail erfolgreich versendet wurde.
        slack_notified: Ob die Slack-Benachrichtigung gesendet wurde.
        duration_seconds: Gesamtdauer der Pipeline-Verarbeitung.
        error: Fehlermeldung bei Verarbeitungsfehler (None bei Erfolg).
        created_at: Zeitstempel des Pipeline-Abschlusses.
    """

    model_config = {"from_attributes": True}

    lead_id: str = Field(
        ...,
        description="Eindeutige ID des verarbeiteten Leads"
    )
    score: int = Field(
        default=0,
        ge=0,
        le=10,
        description="Berechneter Lead-Score (0 bei Fehler, 1-10 bei Erfolg)"
    )
    email_type: str = Field(
        default="",
        description="Typ der erstellten E-Mail ('qualify', 'reject' oder '' bei Fehler)"
    )
    email_sent: bool = Field(
        default=False,
        description="True wenn die E-Mail erfolgreich versendet wurde"
    )
    slack_notified: bool = Field(
        default=False,
        description="True wenn die Slack-Benachrichtigung gesendet wurde"
    )
    duration_seconds: float = Field(
        default=0.0,
        ge=0.0,
        description="Gesamtdauer der Pipeline-Verarbeitung in Sekunden"
    )
    error: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="Fehlermeldung bei Pipeline-Fehler (None bei Erfolg)"
    )
    cost_usd: float = Field(
        default=0.0,
        ge=0.0,
        description="Gesamte API-Kosten fuer diesen Lead in USD"
    )
    created_at: datetime = Field(
        default_factory=datetime.now,
        description="Zeitstempel des Pipeline-Abschlusses"
    )

    @property
    def success(self) -> bool:
        """Gibt True zurueck wenn die Pipeline ohne Fehler abgeschlossen wurde."""
        return self.error is None and self.score > 0
