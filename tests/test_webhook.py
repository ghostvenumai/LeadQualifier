"""
Integrationstests fuer den Lead-Webhook (main.py).

Prueft mit dem Flask-Test-Client:
- HMAC-Signatur-Validierung
- Duplikat-Erkennung (409)
- Rate-Limiting (429)
- Asynchroner Modus (202 + Status-Endpoint)

Die Pipeline selbst wird nie ausgefuehrt - der Executor bzw. die
DB-Zugriffe werden gemockt, damit keine echten API-Calls oder
E-Mails ausgeloest werden.
"""

import hashlib
import hmac
import json
import uuid
from collections import defaultdict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from flask.testing import FlaskClient

import main
from core.models import LicenseInfo


_ACTIVE_LICENSE = LicenseInfo(
    type="demo", active=True, leads_remaining=5, days_remaining=10
)


def _lead_payload() -> dict:
    """Eindeutiger, valider Lead-Body (keine Kollision mit echten Daten)."""
    unique = uuid.uuid4().hex[:10]
    return {
        "name": "Max Mustermann",
        "email": f"max.{unique}@testfirma-{unique}.de",
        "company": f"Testfirma {unique} GmbH",
        "message": "Wir suchen Unterstuetzung bei der Automatisierung unserer Lead-Prozesse.",
    }


@pytest.fixture()
def client(monkeypatch) -> FlaskClient:
    """Test-Client mit frischem Rate-Limit-Store und aktiver Lizenz."""
    main.app.config["TESTING"] = True
    monkeypatch.setattr(main, "_rate_limit_store", defaultdict(list))
    return main.app.test_client()


@pytest.fixture()
def no_duplicates(monkeypatch):
    """DB-Duplikat-Checks liefern 'kein Duplikat' (kein echter DB-Zugriff)."""
    monkeypatch.setattr(main.db, "check_duplicate_email", lambda *_a, **_k: None)
    monkeypatch.setattr(main.db, "check_duplicate_company", lambda *_a, **_k: None)


@pytest.fixture()
def active_license(monkeypatch):
    """Lizenz-Check liefert immer eine aktive Demo-Lizenz."""
    monkeypatch.setattr(main.license_manager, "check", lambda *_a, **_k: _ACTIVE_LICENSE)


class TestWebhookSignature:
    """HMAC-SHA256-Signaturpruefung."""

    def test_missing_signature_rejected(self, client, monkeypatch) -> None:
        monkeypatch.setitem(main.settings.__dict__, "webhook_secret", "test-secret")
        response = client.post("/webhook/lead", json=_lead_payload())
        assert response.status_code == 401

    def test_wrong_signature_rejected(self, client, monkeypatch) -> None:
        monkeypatch.setitem(main.settings.__dict__, "webhook_secret", "test-secret")
        response = client.post(
            "/webhook/lead",
            json=_lead_payload(),
            headers={"X-Webhook-Signature": "sha256=deadbeef"},
        )
        assert response.status_code == 401

    def test_valid_signature_accepted(
        self, client, monkeypatch, no_duplicates, active_license
    ) -> None:
        monkeypatch.setitem(main.settings.__dict__, "webhook_secret", "test-secret")
        body = json.dumps(_lead_payload()).encode()
        signature = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()

        with patch.object(main._pipeline_executor, "submit") as mock_submit:
            response = client.post(
                "/webhook/lead?async=1",
                data=body,
                content_type="application/json",
                headers={"X-Webhook-Signature": f"sha256={signature}"},
            )

        assert response.status_code == 202
        mock_submit.assert_called_once()


class TestWebhookValidation:
    """Body-Validierung und Duplikat-Erkennung."""

    def test_invalid_json_rejected(self, client, monkeypatch) -> None:
        monkeypatch.setitem(main.settings.__dict__, "webhook_secret", None)
        response = client.post(
            "/webhook/lead", data="kein json", content_type="application/json"
        )
        assert response.status_code == 400

    def test_missing_fields_rejected(self, client, monkeypatch) -> None:
        monkeypatch.setitem(main.settings.__dict__, "webhook_secret", None)
        response = client.post("/webhook/lead", json={"name": "Nur ein Name"})
        assert response.status_code == 422

    def test_duplicate_email_returns_409(
        self, client, monkeypatch, active_license
    ) -> None:
        monkeypatch.setitem(main.settings.__dict__, "webhook_secret", None)
        monkeypatch.setattr(
            main.db,
            "check_duplicate_email",
            lambda *_a, **_k: {
                "contacted_at": "2026-06-01T10:00:00+00:00",
                "score": 8,
                "status": "qualified",
            },
        )
        response = client.post("/webhook/lead", json=_lead_payload())
        assert response.status_code == 409
        assert response.get_json()["error"] == "duplicate"

    def test_inactive_license_returns_403(
        self, client, monkeypatch, no_duplicates
    ) -> None:
        monkeypatch.setitem(main.settings.__dict__, "webhook_secret", None)
        monkeypatch.setattr(
            main.license_manager,
            "check",
            lambda *_a, **_k: LicenseInfo(
                type="demo",
                active=False,
                leads_remaining=0,
                days_remaining=5,
                reason="Demo-Limit erreicht (10 Leads).",
            ),
        )
        response = client.post("/webhook/lead", json=_lead_payload())
        assert response.status_code == 403


class TestRateLimit:
    """In-Memory Rate-Limiting (10 Requests/Minute pro IP)."""

    def test_eleventh_request_is_limited(self, client, monkeypatch) -> None:
        monkeypatch.setitem(main.settings.__dict__, "webhook_secret", None)
        for _ in range(10):
            response = client.post(
                "/webhook/lead", data="x", content_type="application/json"
            )
            assert response.status_code != 429
        response = client.post(
            "/webhook/lead", data="x", content_type="application/json"
        )
        assert response.status_code == 429


class TestAsyncWebhook:
    """Asynchroner Modus: 202 Accepted + Status-Endpoint."""

    def test_async_returns_202_with_status_url(
        self, client, monkeypatch, no_duplicates, active_license
    ) -> None:
        monkeypatch.setitem(main.settings.__dict__, "webhook_secret", None)
        with patch.object(main._pipeline_executor, "submit") as mock_submit:
            response = client.post("/webhook/lead?async=1", json=_lead_payload())

        assert response.status_code == 202
        body = response.get_json()
        assert body["accepted"] is True
        assert body["lead_id"]
        assert body["status_url"] == f"/api/leads/{body['lead_id']}/status"
        mock_submit.assert_called_once()

    def test_status_processing_while_not_in_db(self, client, monkeypatch) -> None:
        monkeypatch.setattr(main.db, "get_lead_by_pipeline_id", lambda *_a: None)
        response = client.get("/api/leads/irgendeine-uuid/status")
        assert response.status_code == 200
        assert response.get_json()["status"] == "processing"

    def test_status_completed_when_in_db(self, client, monkeypatch) -> None:
        lead = {
            "lead_id": 1,
            "pipeline_id": "abc-123",
            "company": "Testfirma GmbH",
            "score": 8,
            "status": "qualified",
            "email_type": "qualify",
            "email_sent": True,
            "duration_seconds": 42.5,
            "cost_usd": 0.12,
            "created_at": "2026-06-11T10:00:00+00:00",
        }
        monkeypatch.setattr(main.db, "get_lead_by_pipeline_id", lambda *_a: lead)
        response = client.get("/api/leads/abc-123/status")
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "completed"
        assert body["lead"]["score"] == 8

    def test_sync_param_overrides_async_mode(
        self, client, monkeypatch, no_duplicates, active_license
    ) -> None:
        monkeypatch.setitem(main.settings.__dict__, "webhook_secret", None)
        monkeypatch.setitem(main.settings.__dict__, "webhook_async_mode", True)

        fake_result = MagicMock(
            lead_id="sync-123", score=8, email_type="qualify",
            email_sent=True, duration_seconds=1.0, error=None,
        )
        with patch.object(main.orchestrator, "run", AsyncMock(return_value=fake_result)), \
             patch.object(main.license_manager, "increment_usage"):
            response = client.post("/webhook/lead?sync=1", json=_lead_payload())

        assert response.status_code == 200
        assert response.get_json()["lead_id"] == "sync-123"


class TestRecentLeadsFromDB:
    """GUI-API liest aus der Datenbank statt aus dem In-Memory-Store."""

    def test_recent_leads_come_from_db(self, client, monkeypatch) -> None:
        monkeypatch.setattr(
            main.db,
            "get_recent_leads",
            lambda limit=50: [{"lead_id": 1, "company": "Testfirma GmbH", "score": 7}],
        )
        response = client.get("/api/leads/recent")
        assert response.status_code == 200
        leads = response.get_json()["leads"]
        assert leads == [{"lead_id": 1, "company": "Testfirma GmbH", "score": 7}]

    def test_invalid_limit_rejected(self, client) -> None:
        response = client.get("/api/leads/recent?limit=abc")
        assert response.status_code == 400
