"""
Tests fuer core/license.py - Demo- und Volllizenzverwaltung.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from core.license import LicenseManager
from core.models import LicenseInfo


@pytest.fixture
def mock_db() -> MagicMock:
    """Mock MemoryDB fuer Lizenztests."""
    db = MagicMock()
    db.get_license.return_value = {
        "type": "demo",
        "leads_used": 0,
        "created_at": datetime.now().isoformat(),
    }
    return db


@pytest.fixture
def mock_settings() -> MagicMock:
    settings = MagicMock()
    settings.LICENSE_SERVER_URL = "https://license.ghostvenumai.de/validate"
    settings.DEMO_MAX_LEADS = 10
    settings.DEMO_MAX_DAYS = 14
    return settings


@pytest.fixture
def license_manager(mock_db: MagicMock, mock_settings: MagicMock) -> LicenseManager:
    return LicenseManager(
        db=mock_db,
        license_server_url=mock_settings.LICENSE_SERVER_URL,
        demo_max_leads=10,
        demo_max_days=14,
    )


class TestDemoLicense:
    def test_fresh_demo_is_active(
        self, license_manager: LicenseManager, mock_db: MagicMock
    ) -> None:
        """Frische Demo-Lizenz ist aktiv."""
        mock_db.get_license.return_value = {
            "type": "demo",
            "leads_used": 0,
            "created_at": datetime.now().isoformat(),
        }
        info = license_manager.check(None)
        assert info.active is True
        assert info.type == "demo"

    def test_demo_shows_remaining_leads(
        self, license_manager: LicenseManager, mock_db: MagicMock
    ) -> None:
        """Demo zeigt verbleibende Leads an."""
        mock_db.get_license.return_value = {
            "type": "demo",
            "leads_used": 3,
            "created_at": datetime.now().isoformat(),
        }
        info = license_manager.check(None)
        if info.active:
            assert info.leads_remaining == 7

    def test_demo_expires_after_10_leads(
        self, license_manager: LicenseManager, mock_db: MagicMock
    ) -> None:
        """Demo laeuft nach 10 Leads ab."""
        mock_db.get_license.return_value = {
            "type": "demo",
            "leads_used": 10,
            "created_at": datetime.now().isoformat(),
        }
        info = license_manager.check(None)
        assert info.active is False
        assert "10" in (info.reason or "")

    def test_demo_expires_after_14_days(
        self, license_manager: LicenseManager, mock_db: MagicMock
    ) -> None:
        """Demo laeuft nach 14 Tagen ab."""
        old_date = (datetime.now() - timedelta(days=15)).isoformat()
        mock_db.get_license.return_value = {
            "type": "demo",
            "leads_used": 2,
            "created_at": old_date,
        }
        info = license_manager.check(None)
        assert info.active is False
        assert "14" in (info.reason or "") or "abgelaufen" in (info.reason or "").lower()

    def test_demo_with_9_leads_still_active(
        self, license_manager: LicenseManager, mock_db: MagicMock
    ) -> None:
        """Demo mit 9 Leads ist noch aktiv."""
        mock_db.get_license.return_value = {
            "type": "demo",
            "leads_used": 9,
            "created_at": datetime.now().isoformat(),
        }
        info = license_manager.check(None)
        assert info.active is True
        assert info.leads_remaining == 1

    def test_demo_with_13_days_still_active(
        self, license_manager: LicenseManager, mock_db: MagicMock
    ) -> None:
        """Demo mit 13 vergangenen Tagen ist noch aktiv."""
        recent_date = (datetime.now() - timedelta(days=13)).isoformat()
        mock_db.get_license.return_value = {
            "type": "demo",
            "leads_used": 0,
            "created_at": recent_date,
        }
        info = license_manager.check(None)
        assert info.active is True


class TestFullLicense:
    def test_full_license_server_called(
        self, license_manager: LicenseManager
    ) -> None:
        """Bei Vollversion wird Server kontaktiert."""
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "active": True,
                "type": "starter",
                "leads_remaining": 450,
            }
            mock_post.return_value.raise_for_status = MagicMock()
            info = license_manager.check("valid-license-key-123")
            mock_post.assert_called_once()
            assert info.active is True

    def test_network_error_returns_inactive(
        self, license_manager: LicenseManager
    ) -> None:
        """Netzwerkfehler gibt inaktive Lizenz zurueck."""
        import requests
        with patch("requests.post", side_effect=requests.exceptions.ConnectionError):
            info = license_manager.check("valid-key")
            assert info.active is False
            assert "Verbindung" in (info.reason or "")

    def test_timeout_returns_inactive(
        self, license_manager: LicenseManager
    ) -> None:
        """Timeout gibt inaktive Lizenz zurueck."""
        import requests
        with patch("requests.post", side_effect=requests.exceptions.Timeout):
            info = license_manager.check("valid-key")
            assert info.active is False


class TestMachineId:
    def test_machine_id_is_consistent(self, license_manager: LicenseManager) -> None:
        """Machine-ID ist deterministisch (gleiche Maschine = gleiche ID)."""
        id1 = license_manager._get_machine_id()
        id2 = license_manager._get_machine_id()
        assert id1 == id2

    def test_machine_id_is_hex_string(self, license_manager: LicenseManager) -> None:
        """Machine-ID ist ein Hex-String."""
        machine_id = license_manager._get_machine_id()
        assert len(machine_id) == 64
        int(machine_id, 16)  # Wirft ValueError wenn kein Hex


class TestOnboardingMessage:
    def test_onboarding_message_contains_lead_count(
        self, license_manager: LicenseManager
    ) -> None:
        """Onboarding-Nachricht enthaelt verbleibende Lead-Anzahl."""
        msg = license_manager.get_onboarding_message(leads_remaining=9, days_remaining=14)
        assert "9" in msg

    def test_onboarding_message_mentions_demo(
        self, license_manager: LicenseManager
    ) -> None:
        """Onboarding-Nachricht erwaehnt Demo oder Leads."""
        msg = license_manager.get_onboarding_message(leads_remaining=9, days_remaining=14)
        assert "Demo" in msg or "Lead" in msg or "lead" in msg.lower()

    def test_onboarding_message_is_german(
        self, license_manager: LicenseManager
    ) -> None:
        """Onboarding-Nachricht ist auf Deutsch."""
        msg = license_manager.get_onboarding_message(leads_remaining=9, days_remaining=14)
        german_keywords = ["Sie", "Leads", "verfuegbar", "qualifiziert"]
        assert any(kw in msg for kw in german_keywords)


class TestLicenseInfoModel:
    def test_demo_license_info_valid(self) -> None:
        info = LicenseInfo(
            type="demo",
            active=True,
            leads_remaining=7,
            days_remaining=10,
        )
        assert info.type == "demo"
        assert info.active is True

    def test_inactive_license_has_reason(self) -> None:
        info = LicenseInfo(
            type="demo",
            active=False,
            leads_remaining=0,
            days_remaining=0,
            reason="Demo-Limit erreicht (10 Leads)",
        )
        assert info.reason is not None
        assert len(info.reason) > 0

    def test_enterprise_license_no_lead_limit(self) -> None:
        info = LicenseInfo(
            type="enterprise",
            active=True,
            leads_remaining=None,
            days_remaining=None,
        )
        assert info.leads_remaining is None
