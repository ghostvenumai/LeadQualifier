"""
Tests fuer core/blackboard.py - LeadBlackboard SharedMemory.
"""

import time
from datetime import datetime

import pytest

from core.blackboard import LeadBlackboard


@pytest.fixture
def board() -> LeadBlackboard:
    """Erstellt ein minimales LeadBlackboard fuer Tests."""
    return LeadBlackboard(
        lead_id="test-123",
        name="Max Mustermann",
        email="max@beispiel.de",
        company="Beispiel GmbH",
        message="Wir suchen eine Lösung für unser Lead-Management.",
    )


class TestLeadBlackboardInit:
    def test_required_fields(self, board: LeadBlackboard) -> None:
        assert board.lead_id == "test-123"
        assert board.name == "Max Mustermann"
        assert board.company == "Beispiel GmbH"

    def test_default_values(self, board: LeadBlackboard) -> None:
        assert board.score == 0
        assert board.score_reasoning == ""
        assert board.draft_email == ""
        assert board.email_type == ""
        assert board.review_passed is False
        assert board.final_email == ""
        assert board.research == {}
        assert board.score_details == {}
        assert board.agent_log == []

    def test_created_at_is_datetime(self, board: LeadBlackboard) -> None:
        assert isinstance(board.created_at, datetime)

    def test_two_boards_have_different_created_at(self) -> None:
        b1 = LeadBlackboard("id1", "A", "a@b.de", "Firma A", "Msg A")
        time.sleep(0.01)
        b2 = LeadBlackboard("id2", "B", "b@c.de", "Firma B", "Msg B")
        assert b2.created_at >= b1.created_at


class TestLeadBlackboardLog:
    def test_log_appends_entry(self, board: LeadBlackboard) -> None:
        board.log("TestAgent", "test_action")
        assert len(board.agent_log) == 1

    def test_log_entry_structure(self, board: LeadBlackboard) -> None:
        board.log("ScoringAgent", "scoring_complete: score=8")
        entry = board.agent_log[0]
        assert entry["agent"] == "ScoringAgent"
        assert entry["action"] == "scoring_complete: score=8"
        assert "timestamp" in entry

    def test_log_multiple_entries(self, board: LeadBlackboard) -> None:
        board.log("ResearchAgent", "research_started")
        board.log("ResearchAgent", "research_complete")
        board.log("ScoringAgent", "scoring_complete: score=7")
        assert len(board.agent_log) == 3

    def test_log_timestamp_is_iso_format(self, board: LeadBlackboard) -> None:
        board.log("WriterAgent", "email_drafted")
        ts = board.agent_log[0]["timestamp"]
        # Sollte ohne Exception parsen lassen
        datetime.fromisoformat(ts)

    def test_log_does_not_leak_email(self, board: LeadBlackboard) -> None:
        board.log("OrchestratorAgent", "pipeline_started")
        entry_str = str(board.agent_log[0])
        assert board.email not in entry_str


class TestLeadBlackboardScoring:
    def test_score_can_be_set(self, board: LeadBlackboard) -> None:
        board.score = 8
        assert board.score == 8

    def test_score_reasoning_can_be_set(self, board: LeadBlackboard) -> None:
        board.score_reasoning = "Entscheider erkannt, professionelle Website"
        assert "Entscheider" in board.score_reasoning

    def test_score_details_can_be_set(self, board: LeadBlackboard) -> None:
        board.score_details = {"decision_maker": 3, "company_size": 2}
        assert board.score_details["decision_maker"] == 3


class TestLeadBlackboardResearch:
    def test_research_can_be_populated(self, board: LeadBlackboard) -> None:
        board.research = {
            "industry": "IT",
            "size": "50-200",
            "is_decision_maker": True,
            "website_quality": "professional",
            "linkedin_url": "https://www.linkedin.com/company/beispiel-gmbh",
        }
        assert board.research["industry"] == "IT"
        assert board.research["is_decision_maker"] is True


class TestLeadBlackboardReview:
    def test_review_passed_can_be_set(self, board: LeadBlackboard) -> None:
        board.review_passed = True
        board.final_email = "Sehr geehrter Herr Mustermann, ..."
        assert board.review_passed is True
        assert len(board.final_email) > 0

    def test_review_feedback_can_be_set(self, board: LeadBlackboard) -> None:
        board.review_feedback = "E-Mail zu kurz, Firmenname fehlt"
        assert "Firmenname" in board.review_feedback
