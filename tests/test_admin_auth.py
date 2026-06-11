"""
Tests fuer die Admin-Authentifizierung der Konfigurations-Endpunkte.

Prueft:
- Ohne ADMIN_TOKEN: Zugriff nur von localhost (Fail-Closed nach aussen)
- Mit ADMIN_TOKEN: Header-Pflicht mit timing-sicherem Vergleich
- /api/settings/config gibt keine Secret-Inhalte mehr zurueck
"""

import pytest
from flask.testing import FlaskClient

import main


_REMOTE_ADDR = {"REMOTE_ADDR": "203.0.113.5"}  # Beispiel-IP ausserhalb von localhost


@pytest.fixture()
def client() -> FlaskClient:
    """Flask Test-Client fuer die App."""
    main.app.config["TESTING"] = True
    return main.app.test_client()


class TestAdminAuthWithoutToken:
    """Verhalten wenn kein ADMIN_TOKEN konfiguriert ist."""

    def test_localhost_allowed(self, client: FlaskClient, monkeypatch) -> None:
        monkeypatch.delenv("ADMIN_TOKEN", raising=False)
        response = client.get("/api/prompts")
        assert response.status_code == 200

    def test_remote_denied(self, client: FlaskClient, monkeypatch) -> None:
        monkeypatch.delenv("ADMIN_TOKEN", raising=False)
        response = client.get("/api/prompts", environ_base=_REMOTE_ADDR)
        assert response.status_code == 403

    def test_remote_denied_settings_save(self, client: FlaskClient, monkeypatch) -> None:
        monkeypatch.delenv("ADMIN_TOKEN", raising=False)
        response = client.post(
            "/api/settings/save",
            json={"QUALIFY_THRESHOLD": "8"},
            environ_base=_REMOTE_ADDR,
        )
        assert response.status_code == 403


class TestAdminAuthWithToken:
    """Verhalten wenn ADMIN_TOKEN konfiguriert ist."""

    def test_missing_header_denied(self, client: FlaskClient, monkeypatch) -> None:
        monkeypatch.setenv("ADMIN_TOKEN", "geheimes-token-123")
        response = client.get("/api/prompts")
        assert response.status_code == 401

    def test_wrong_token_denied(self, client: FlaskClient, monkeypatch) -> None:
        monkeypatch.setenv("ADMIN_TOKEN", "geheimes-token-123")
        response = client.get(
            "/api/prompts", headers={"X-Admin-Token": "falsches-token"}
        )
        assert response.status_code == 401

    def test_correct_token_allowed_remote(self, client: FlaskClient, monkeypatch) -> None:
        monkeypatch.setenv("ADMIN_TOKEN", "geheimes-token-123")
        response = client.get(
            "/api/prompts",
            headers={"X-Admin-Token": "geheimes-token-123"},
            environ_base=_REMOTE_ADDR,
        )
        assert response.status_code == 200


class TestConfigNoSecretLeak:
    """/api/settings/config darf keine Secret-Inhalte zurueckgeben."""

    def test_no_secret_prefix_in_response(self, client: FlaskClient, monkeypatch) -> None:
        monkeypatch.delenv("ADMIN_TOKEN", raising=False)
        fake_key = "sk-ant-test-supersecret-abcdef123456"
        monkeypatch.setenv("ANTHROPIC_API_KEY", fake_key)
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcdwxyzabcdwxyz")

        response = client.get("/api/settings/config")
        assert response.status_code == 200

        body = response.get_data(as_text=True)
        # Weder der volle Wert noch ein Praefix davon darf auftauchen
        assert fake_key not in body
        assert fake_key[:8] not in body
        assert "abcdwxyz" not in body

        config = response.get_json()["config"]
        assert config["anthropic_api_key"] == "konfiguriert ✓"
        assert config["gmail_app_password"] == "konfiguriert ✓"

    def test_unset_value_is_null(self, client: FlaskClient, monkeypatch) -> None:
        monkeypatch.delenv("ADMIN_TOKEN", raising=False)
        monkeypatch.delenv("CRM_WEBHOOK_URL", raising=False)
        response = client.get("/api/settings/config")
        assert response.get_json()["config"]["crm_webhook_url"] is None
