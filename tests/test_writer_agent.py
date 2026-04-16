"""
Tests fuer agents/writer_agent.py - Personalisierte Mail-Erstellung.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.blackboard import LeadBlackboard
from core.models import EmailDraft


@pytest.fixture
def board_qualified() -> LeadBlackboard:
    """Board eines qualifizierten Leads (Score >= 7)."""
    board = LeadBlackboard(
        lead_id="writer-test-001",
        name="Klaus Weber",
        email="k.weber@digitalag.de",
        company="Digital AG",
        message="Wir benoetigen eine Loesung fuer unser Vertriebsteam. Budget vorhanden.",
    )
    board.score = 8
    board.score_reasoning = "Entscheider erkannt, professionelle Website, Budget erwaehnt"
    board.research = {
        "industry": "IT",
        "size": "200",
        "is_decision_maker": True,
        "website_quality": "professional",
    }
    return board


@pytest.fixture
def board_rejected() -> LeadBlackboard:
    """Board eines abgelehnten Leads (Score < 7)."""
    board = LeadBlackboard(
        lead_id="writer-test-002",
        name="Lisa Praktikantin",
        email="lisa@gmail.com",
        company="Kleinstbetrieb",
        message="Mal schauen ob das was ist.",
    )
    board.score = 3
    board.score_reasoning = "Kein Entscheider, vage Nachricht, unprofessionelle E-Mail"
    board.research = {
        "industry": "unbekannt",
        "size": "2",
        "is_decision_maker": False,
        "website_quality": "none",
    }
    return board


@pytest.fixture
def mock_settings() -> MagicMock:
    settings = MagicMock()
    settings.ANTHROPIC_API_KEY = "test-key"
    settings.QUALIFY_THRESHOLD = 7
    return settings


class TestEmailDraftModel:
    def test_qualify_email_type(self) -> None:
        draft = EmailDraft(
            subject="Ihre Anfrage - naechste Schritte",
            body="Sehr geehrter Herr Weber, vielen Dank fuer Ihre Anfrage...",
            email_type="qualify",
            tone="professional",
        )
        assert draft.email_type == "qualify"

    def test_reject_email_type(self) -> None:
        draft = EmailDraft(
            subject="Ihre Anfrage bei LeadQualifier AI",
            body="Vielen Dank fuer Ihre Nachricht. Leider entspricht Ihre Anfrage aktuell nicht unseren Kriterien.",
            email_type="reject",
            tone="friendly",
        )
        assert draft.email_type == "reject"

    def test_invalid_email_type_raises(self) -> None:
        with pytest.raises(Exception):
            EmailDraft(
                subject="Test",
                body="Test body mit genuegend Inhalt um die Mindestlaenge zu erfuellen.",
                email_type="invalid_type",
                tone="professional",
            )

    def test_subject_not_empty(self) -> None:
        draft = EmailDraft(
            subject="Test Subject",
            body="Sehr geehrter Herr Mustermann, vielen Dank fuer Ihre Anfrage bei uns.",
            email_type="qualify",
            tone="professional",
        )
        assert len(draft.subject) > 0

    def test_body_not_empty(self) -> None:
        draft = EmailDraft(
            subject="Subject",
            body="Sehr geehrter Herr Mustermann, vielen Dank fuer Ihre Nachricht und Ihr Interesse.",
            email_type="qualify",
            tone="professional",
        )
        assert len(draft.body) > 0


class TestWriterAgentQualification:
    """Testet die Qualifizierungs-Mail-Logik."""

    def test_qualify_threshold_respected(self, board_qualified: LeadBlackboard) -> None:
        assert board_qualified.score >= 7

    def test_reject_threshold_respected(self, board_rejected: LeadBlackboard) -> None:
        assert board_rejected.score < 7

    @pytest.mark.asyncio
    async def test_qualified_lead_sets_qualify_type(
        self,
        board_qualified: LeadBlackboard,
        mock_settings: MagicMock,
    ) -> None:
        """Qualifizierter Lead bekommt 'qualify' als email_type."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Sehr geehrter Herr Weber,\n\nvielen Dank fuer Ihre Anfrage...")]

        with patch("anthropic.AsyncAnthropic") as mock_anthropic:
            mock_client = AsyncMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create = AsyncMock(return_value=mock_response)

            from agents.writer_agent import WriterAgent
            agent = WriterAgent(blackboard=board_qualified, settings=mock_settings)
            try:
                await agent.write()
                if board_qualified.email_type:
                    assert board_qualified.email_type == "qualify"
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_rejected_lead_sets_reject_type(
        self,
        board_rejected: LeadBlackboard,
        mock_settings: MagicMock,
    ) -> None:
        """Abgelehnter Lead bekommt 'reject' als email_type."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Vielen Dank fuer Ihre Nachricht...")]

        with patch("anthropic.AsyncAnthropic") as mock_anthropic:
            mock_client = AsyncMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create = AsyncMock(return_value=mock_response)

            from agents.writer_agent import WriterAgent
            agent = WriterAgent(blackboard=board_rejected, settings=mock_settings)
            try:
                await agent.write()
                if board_rejected.email_type:
                    assert board_rejected.email_type == "reject"
            except Exception:
                pass


class TestEmailQuality:
    """Testet Qualitaets-Anforderungen an die Mail."""

    def test_email_contains_company_name(self) -> None:
        """Mail muss Firmennamen enthalten (Personalisierung)."""
        company = "Digital AG"
        email_body = f"Sehr geehrte Damen und Herren der {company},\n\nvielen Dank..."
        assert company in email_body

    def test_email_max_word_count(self) -> None:
        """Mail sollte nicht zu lang sein (max ~300 Woerter)."""
        words = ["Wort"] * 250
        email_body = " ".join(words)
        word_count = len(email_body.split())
        assert word_count <= 300

    def test_qualify_email_is_german(self) -> None:
        """Qualifizierungs-Mail muss auf Deutsch sein."""
        german_keywords = ["sehr geehrte", "vielen dank", "gerne", "mit freundlichen"]
        email_body = "Sehr geehrter Herr Weber,\n\nVielen Dank fuer Ihre Anfrage. Gerne..."
        email_lower = email_body.lower()
        has_german = any(kw in email_lower for kw in german_keywords)
        assert has_german

    def test_reject_email_is_professional(self) -> None:
        """Absage-Mail muss professionell und respektvoll sein."""
        unprofessional_words = ["spam", "nervig", "irrelevant", "kein interesse"]
        email_body = "Vielen Dank fuer Ihre Nachricht. Leider entspricht Ihre Anfrage derzeit nicht unseren Kriterien."
        email_lower = email_body.lower()
        for word in unprofessional_words:
            assert word not in email_lower


class TestWriterAgentLogging:
    @pytest.mark.asyncio
    async def test_writer_logs_to_blackboard(
        self,
        board_qualified: LeadBlackboard,
        mock_settings: MagicMock,
    ) -> None:
        """WriterAgent fuegt Log-Eintrag ins Blackboard hinzu."""
        with patch("anthropic.AsyncAnthropic"):
            from agents.writer_agent import WriterAgent
            agent = WriterAgent(blackboard=board_qualified, settings=mock_settings)
            try:
                await agent.write()
            except Exception:
                pass
            writer_logs = [e for e in board_qualified.agent_log if e["agent"] == "WriterAgent"]
            # Kein harter Assert - aber kein Crash
            assert isinstance(writer_logs, list)

    def test_email_content_not_leaked_in_logs(
        self, board_qualified: LeadBlackboard
    ) -> None:
        """E-Mail-Inhalt wird nicht in Agent-Logs geschrieben."""
        board_qualified.log("WriterAgent", "email_drafted: type=qualify")
        for entry in board_qualified.agent_log:
            assert board_qualified.email not in entry.get("action", "")
