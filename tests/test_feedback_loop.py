"""
Tests fuer den geschlossenen Scoring-Feedback-Loop.

Prueft:
- save_scoring_feedback laedt den vorhergesagten Score aus der leads-Tabelle
- get_scoring_calibration aggregiert korrekt und respektiert min_samples
- ScoringAgent baut aus den Aggregaten einen Kalibrierungs-Hinweis
"""

import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from core.memory import MemoryDB


@pytest.fixture()
def db(tmp_path) -> MemoryDB:
    """Frische MemoryDB in einem Temp-Verzeichnis."""
    return MemoryDB(str(tmp_path / "test.db"))


def _insert_lead(db: MemoryDB, score: int) -> int:
    """Fuegt einen minimalen Lead-Datensatz ein und gibt die Row-ID zurueck."""
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db._db_path)
    try:
        cursor = conn.execute(
            "INSERT INTO leads (email_hash, company_name, score, status, contacted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("hash-" + str(score), "Testfirma GmbH", score, "qualified", now),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def _read_feedback(db: MemoryDB) -> list[tuple]:
    """Liest alle Feedback-Zeilen (predicted_score, actual_outcome)."""
    conn = sqlite3.connect(db._db_path)
    try:
        return conn.execute(
            "SELECT predicted_score, actual_outcome FROM scoring_feedback"
        ).fetchall()
    finally:
        conn.close()


class TestSaveScoringFeedback:
    """save_scoring_feedback ohne expliziten Score."""

    def test_score_loaded_from_leads_table(self, db: MemoryDB) -> None:
        lead_id = _insert_lead(db, score=8)
        db.save_scoring_feedback(lead_id=lead_id, actual_outcome="converted")

        rows = _read_feedback(db)
        assert rows == [(8, "converted")]

    def test_unknown_lead_falls_back_to_zero(self, db: MemoryDB) -> None:
        db.save_scoring_feedback(lead_id=99999, actual_outcome="rejected")
        rows = _read_feedback(db)
        assert rows == [(0, "rejected")]

    def test_explicit_score_kept(self, db: MemoryDB) -> None:
        db.save_scoring_feedback(
            lead_id=1, predicted_score=6, actual_outcome="no_response"
        )
        rows = _read_feedback(db)
        assert rows == [(6, "no_response")]


class TestScoringCalibration:
    """Aggregation des Feedbacks zu Kalibrierungs-Statistiken."""

    def _add_feedback(self, db: MemoryDB, score: int, outcome: str) -> None:
        lead_id = _insert_lead(db, score=score)
        db.save_scoring_feedback(lead_id=lead_id, actual_outcome=outcome)

    def test_returns_none_below_min_samples(self, db: MemoryDB) -> None:
        self._add_feedback(db, 8, "converted")
        self._add_feedback(db, 7, "rejected")
        assert db.get_scoring_calibration(min_samples=5) is None

    def test_aggregates_correctly(self, db: MemoryDB) -> None:
        # 3 konvertiert (Scores 8, 9, 6), 3 nicht konvertiert (Scores 7, 8, 4)
        self._add_feedback(db, 8, "converted")
        self._add_feedback(db, 9, "converted")
        self._add_feedback(db, 6, "converted")
        self._add_feedback(db, 7, "rejected")
        self._add_feedback(db, 8, "no_response")
        self._add_feedback(db, 4, "rejected")

        stats = db.get_scoring_calibration(qualify_threshold=7, min_samples=5)

        assert stats is not None
        assert stats["sample_count"] == 6
        assert stats["avg_score_converted"] == pytest.approx((8 + 9 + 6) / 3)
        assert stats["avg_score_not_converted"] == pytest.approx((7 + 8 + 4) / 3)
        # Score >= 7: 8, 9, 7, 8 -> davon nicht konvertiert: 7, 8
        assert stats["qualified_total"] == 4
        assert stats["false_positives"] == 2
        # Score < 7 aber konvertiert: 6
        assert stats["false_negatives"] == 1


class TestCalibrationHintInPrompt:
    """ScoringAgent injiziert Kalibrierungs-Hinweise in den Prompt."""

    def _make_agent(self, db) -> "ScoringAgent":  # noqa: F821
        from unittest.mock import patch
        from agents.scoring_agent import ScoringAgent
        from core.blackboard import LeadBlackboard

        blackboard = LeadBlackboard(
            lead_id="test-123",
            name="Max Mustermann",
            email="max@testfirma.de",
            company="Testfirma GmbH",
            message="Wir suchen Unterstuetzung bei der Automatisierung.",
        )
        settings = MagicMock()
        settings.anthropic_api_key = "test-key"
        settings.qualify_threshold = 7

        with patch("anthropic.AsyncAnthropic"):
            return ScoringAgent(blackboard=blackboard, settings=settings, db=db)

    def test_hint_included_with_enough_feedback(self, db: MemoryDB) -> None:
        for score, outcome in [
            (8, "converted"), (9, "converted"), (7, "rejected"),
            (8, "no_response"), (5, "rejected"), (6, "converted"),
        ]:
            lead_id = _insert_lead(db, score=score)
            db.save_scoring_feedback(lead_id=lead_id, actual_outcome=outcome)

        agent = self._make_agent(db)
        prompt = agent._build_user_prompt()

        assert "Kalibrierung aus realem Vertriebs-Feedback" in prompt
        assert "n=6" in prompt

    def test_no_hint_without_db(self, db: MemoryDB) -> None:
        agent = self._make_agent(None)
        prompt = agent._build_user_prompt()
        assert "Kalibrierung" not in prompt

    def test_no_hint_with_too_few_samples(self, db: MemoryDB) -> None:
        lead_id = _insert_lead(db, score=8)
        db.save_scoring_feedback(lead_id=lead_id, actual_outcome="converted")

        agent = self._make_agent(db)
        prompt = agent._build_user_prompt()
        assert "Kalibrierung" not in prompt
