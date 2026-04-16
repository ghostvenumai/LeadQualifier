"""
LicenseManager: Demo- und Volllizenz-Verwaltung fuer LeadQualifier AI.

Demo: laeuft ab nach 10 Leads ODER 14 Tagen (was zuerst eintritt).
Vollversion: Server-Validierung gegen GhostVenumAI Lizenzserver.
"""

import hashlib
import logging
import socket
import uuid
from datetime import datetime
from typing import Optional

import requests

from core.memory import MemoryDB
from core.models import LicenseInfo

logger = logging.getLogger(__name__)


class LicenseManager:
    """Prueft und verwaltet Demo- und Volllizenzen."""

    def __init__(self, db: MemoryDB, license_server_url: str, demo_max_leads: int = 10, demo_max_days: int = 14) -> None:
        """
        Args:
            db: MemoryDB-Instanz fuer Lizenz-Daten.
            license_server_url: URL des GhostVenumAI Lizenzservers.
            demo_max_leads: Maximale Leads im Demo-Modus (default: 10).
            demo_max_days: Maximale Tage im Demo-Modus (default: 14).
        """
        self.db = db
        self.license_server_url = license_server_url
        self.demo_max_leads = demo_max_leads
        self.demo_max_days = demo_max_days

    def check(self, key: Optional[str] = None) -> LicenseInfo:
        """
        Prueft die Lizenz. Gibt LicenseInfo zurueck.

        Ohne Key: Demo-Modus.
        Mit Key: Server-Validierung fuer Vollversion.

        Args:
            key: Lizenzschluessel (None = Demo).

        Returns:
            LicenseInfo mit Status und verbleibenden Kontingenten.
        """
        if not key:
            return self._check_demo()
        return self._check_full(key)

    def _check_demo(self) -> LicenseInfo:
        """Prueft Demo-Limits: 10 Leads ODER 14 Tage."""
        try:
            info = self.db.get_license("demo")
            if not info:
                # Demo-Lizenz initialisieren wenn noch nicht vorhanden
                try:
                    from core.memory import MemoryDB as _MemDB  # noqa
                    self.db.save_license("demo", "demo")
                except Exception:
                    pass
                info = self.db.get_license("demo") or {}

            leads_used: int = info.get("leads_used", 0)
            created_at_str: str = info.get("created_at", datetime.now().isoformat())

            try:
                created_at = datetime.fromisoformat(created_at_str)
                # Timezone-aware -> naive konvertieren fuer Vergleich
                if created_at.tzinfo is not None:
                    created_at = created_at.replace(tzinfo=None)
            except (ValueError, AttributeError):
                created_at = datetime.now()

            days_used = (datetime.now() - created_at).days
            days_remaining = max(0, self.demo_max_days - days_used)
            leads_remaining = max(0, self.demo_max_leads - leads_used)

            if days_used > self.demo_max_days:
                logger.info("Demo abgelaufen: %d Tage vergangen", days_used)
                return LicenseInfo(
                    type="demo",
                    active=False,
                    leads_remaining=0,
                    days_remaining=0,
                    reason=f"Demo abgelaufen ({self.demo_max_days} Tage)",
                )

            if leads_used >= self.demo_max_leads:
                logger.info("Demo-Lead-Limit erreicht: %d Leads", leads_used)
                return LicenseInfo(
                    type="demo",
                    active=False,
                    leads_remaining=0,
                    days_remaining=days_remaining,
                    reason=f"Demo-Limit erreicht ({self.demo_max_leads} Leads)",
                )

            return LicenseInfo(
                type="demo",
                active=True,
                leads_remaining=leads_remaining,
                days_remaining=days_remaining,
            )

        except Exception as exc:
            logger.error("Fehler beim Demo-Check: %s", type(exc).__name__)
            return LicenseInfo(
                type="demo",
                active=False,
                leads_remaining=0,
                days_remaining=0,
                reason="Interner Fehler bei Lizenzpruefung",
            )

    def _check_full(self, key: str) -> LicenseInfo:
        """Vollversion: Validierung gegen GhostVenumAI Lizenzserver."""
        try:
            response = requests.post(
                self.license_server_url,
                json={"key": key, "machine_id": self._get_machine_id()},
                timeout=5,
            )
            response.raise_for_status()
            data = response.json()

            license_type = data.get("type", "starter")
            active = data.get("active", False)

            return LicenseInfo(
                type=license_type,
                active=active,
                leads_remaining=data.get("leads_remaining"),
                days_remaining=data.get("days_remaining"),
                reason=data.get("reason"),
            )

        except requests.exceptions.Timeout:
            logger.warning("Lizenzserver Timeout")
            return LicenseInfo(
                type="starter",
                active=False,
                reason="Keine Verbindung zum Lizenzserver (Timeout)",
            )
        except requests.exceptions.ConnectionError:
            logger.warning("Lizenzserver nicht erreichbar")
            return LicenseInfo(
                type="starter",
                active=False,
                reason="Keine Verbindung zum Lizenzserver",
            )
        except Exception as exc:
            logger.error("Lizenzserver-Fehler: %s", type(exc).__name__)
            return LicenseInfo(
                type="starter",
                active=False,
                reason="Fehler bei Lizenzvalidierung",
            )

    def _get_machine_id(self) -> str:
        """
        Erstellt eine deterministische Machine-ID aus Hostname und MAC-Adresse.

        Returns:
            SHA-256 Hash als Hex-String.
        """
        try:
            hostname = socket.gethostname()
            mac = str(uuid.getnode())
            raw = f"{hostname}:{mac}"
            return hashlib.sha256(raw.encode()).hexdigest()
        except Exception:
            return hashlib.sha256(b"unknown-machine").hexdigest()

    def get_onboarding_message(self, leads_remaining: int, days_remaining: int) -> str:
        """
        Gibt die Onboarding-Nachricht nach dem ersten Lead zurueck.

        Args:
            leads_remaining: Verbleibende Demo-Leads.
            days_remaining: Verbleibende Demo-Tage.

        Returns:
            Onboarding-Nachricht als String.
        """
        return (
            f"Ihr erster Lead wurde erfolgreich qualifiziert! "
            f"Sie haben noch {leads_remaining} von {self.demo_max_leads} Demo-Leads verfuegbar "
            f"({days_remaining} Tage verbleibend). "
            f"Moechten Sie die Vollversion freischalten?"
        )

    def increment_usage(self, key: Optional[str] = None) -> None:
        """Erhoeht den Lead-Zaehler nach erfolgreicher Verarbeitung."""
        license_key = key or "demo"
        try:
            self.db.increment_lead_count(license_key)
        except Exception as exc:
            logger.error("Fehler beim Erhoehen des Lead-Zaehlers: %s", type(exc).__name__)
