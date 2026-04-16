"""
MemoryDB: SQLite-basiertes Persistenzmodul fuer LeadQualifier AI.

Verwaltet vier Tabellen:
- companies: Firmendaten-Cache (30 Tage Gueltigkeit)
- leads: Lead-History mit SHA-256-gehashten E-Mails (DSGVO)
- scoring_feedback: Feedback-Loop fuer Score-Verbesserungen
- license: Lizenz-Status und Nutzungsstatistiken

DSGVO-Konformitaet:
    E-Mail-Adressen werden als SHA-256-Hash gespeichert.
    Kein Klartext-Email in der Datenbank.
    Parameterized Queries gegen SQL-Injection.
"""

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Generator, Optional

from core.blackboard import LeadBlackboard

logger = logging.getLogger(__name__)

# Cache-Gueltigkeit fuer Firmendaten in Tagen
_COMPANY_CACHE_TTL_DAYS: int = 30


class MemoryDB:
    """
    SQLite-Datenbankschicht fuer alle persistenten Daten des Systems.

    Verwaltet Firmendaten-Cache, Lead-History (DSGVO-konform mit
    gehashten E-Mails), Scoring-Feedback und Lizenzinformationen.

    Thread-Safety: Verwendet check_same_thread=False fuer Flask-Kompatibilitaet.
    Jede Methode erstellt ihre eigene Connection via Context-Manager.

    Attributes:
        _db_path: Pfad zur SQLite-Datenbankdatei.
    """

    def __init__(self, db_path: str = "leadqualifier.db") -> None:
        """
        Initialisiert die MemoryDB und erstellt die Datenbank falls noetig.

        Args:
            db_path: Pfad zur SQLite-Datenbankdatei.
                     Default: 'leadqualifier.db' im aktuellen Verzeichnis.
        """
        self._db_path: str = db_path
        self.init_db()
        logger.info("MemoryDB: Datenbank initialisiert unter '%s'.", db_path)

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """
        Context-Manager fuer SQLite-Verbindungen.

        Erstellt eine neue Verbindung, gibt sie als Context zurueck und
        schliesst sie nach Verwendung. Bei Fehlern wird automatisch ein
        Rollback durchgefuehrt.

        Yields:
            Offene SQLite-Connection mit aktiviertem Row-Factory.
        """
        conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
            # Kein PARSE_DECLTYPES - wir verwalten Timestamps als TEXT selbst
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")  # Write-Ahead Logging fuer Concurrency
        conn.execute("PRAGMA foreign_keys=ON;")

        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        """
        Erstellt alle Tabellen falls sie noch nicht existieren.

        Idempotent: Kann mehrfach aufgerufen werden ohne Datenverlust.
        Verwendet IF NOT EXISTS fuer alle CREATE-Statements.
        """
        with self._get_connection() as conn:
            conn.executescript("""
                -- Firma-Cache (30 Tage gueltig)
                CREATE TABLE IF NOT EXISTS companies (
                    domain TEXT PRIMARY KEY,
                    name TEXT,
                    industry TEXT,
                    size TEXT,
                    research_data JSON,
                    last_updated TIMESTAMP
                );

                -- Lead-History (DSGVO-konform: email als SHA-256 Hash)
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email_hash TEXT,
                    company_domain TEXT,
                    score INTEGER,
                    status TEXT,
                    contacted_at TIMESTAMP,
                    became_customer BOOLEAN DEFAULT FALSE,
                    pipeline_log JSON
                );

                -- Scoring-Feedback fuer ML-Loop
                CREATE TABLE IF NOT EXISTS scoring_feedback (
                    lead_id INTEGER,
                    predicted_score INTEGER,
                    actual_outcome TEXT,
                    feedback_date TIMESTAMP
                );

                -- Lizenzinformationen
                CREATE TABLE IF NOT EXISTS license (
                    key_hash TEXT PRIMARY KEY,
                    type TEXT,
                    created_at TIMESTAMP,
                    leads_used INTEGER DEFAULT 0,
                    metadata JSON
                );

                -- Index fuer haeufige Abfragen
                CREATE INDEX IF NOT EXISTS idx_leads_email_hash
                    ON leads(email_hash);
                CREATE INDEX IF NOT EXISTS idx_leads_company_domain
                    ON leads(company_domain);
                CREATE INDEX IF NOT EXISTS idx_companies_last_updated
                    ON companies(last_updated);
            """)
            logger.debug("MemoryDB: Tabellen und Indizes sichergestellt.")

    def get_company_cache(self, domain: str) -> Optional[dict]:
        """
        Holt gecachte Firmendaten aus der Datenbank.

        Gibt None zurueck wenn kein Eintrag existiert oder der Eintrag
        aelter als 30 Tage ist (Cache-Invalidierung).

        Args:
            domain: Domainnamen des Unternehmens (z.B. "beispiel.de").

        Returns:
            Research-Daten als Dictionary oder None bei Cache-Miss.
        """
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT research_data, last_updated FROM companies WHERE domain = ?",
                    (domain,),
                ).fetchone()

            if row is None:
                logger.debug("MemoryDB: Cache-Miss fuer Domain '%s'.", domain)
                return None

            # Cache-Alter pruefen
            last_updated_str = row["last_updated"]
            if not last_updated_str:
                return None

            try:
                last_updated = datetime.fromisoformat(last_updated_str)
                # Timezone-aware Vergleich
                if last_updated.tzinfo is None:
                    last_updated = last_updated.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                if now - last_updated > timedelta(days=_COMPANY_CACHE_TTL_DAYS):
                    logger.debug(
                        "MemoryDB: Cache-Eintrag fuer Domain abgelaufen (>%d Tage).",
                        _COMPANY_CACHE_TTL_DAYS,
                    )
                    return None
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "MemoryDB: Ungultiges Datums-Format im Cache: %s",
                    type(exc).__name__,
                )
                return None

            research_data = row["research_data"]
            if isinstance(research_data, str):
                return json.loads(research_data)
            elif isinstance(research_data, dict):
                return research_data
            return None

        except Exception as exc:
            logger.error(
                "MemoryDB: get_company_cache fehlgeschlagen fuer Domain: %s",
                type(exc).__name__,
                exc_info=True,
            )
            return None

    def set_company_cache(self, domain: str, data: dict) -> None:
        """
        Speichert oder aktualisiert Firmendaten im Cache.

        Verwendet UPSERT (INSERT OR REPLACE) fuer idempotentes Speichern.

        Args:
            domain: Domainnamen des Unternehmens.
            data: Research-Daten als Dictionary.
        """
        try:
            now = datetime.now(timezone.utc).isoformat()
            research_json = json.dumps(data, ensure_ascii=False, default=str)
            name = data.get("name", "") or ""
            industry = data.get("industry", "") or ""
            size = data.get("size", "") or ""

            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO companies
                        (domain, name, industry, size, research_data, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (domain, name, industry, size, research_json, now),
                )

            logger.debug("MemoryDB: Cache gesetzt fuer Domain '%s'.", domain)

        except Exception as exc:
            logger.error(
                "MemoryDB: set_company_cache fehlgeschlagen: %s",
                type(exc).__name__,
                exc_info=True,
            )

    def save_lead(self, blackboard: LeadBlackboard) -> None:
        """
        Speichert einen verarbeiteten Lead in der Datenbank.

        E-Mail-Adresse wird als SHA-256-Hash gespeichert (DSGVO-Minimalprinzip).
        Der vollstaendige Pipeline-Log wird als JSON persistiert.

        Args:
            blackboard: Vollstaendig verarbeitetes Blackboard.
        """
        try:
            email_hash = self.hash_email(blackboard.email) if blackboard.email else ""
            company_domain = self._extract_domain(blackboard.research.get("website_url", ""))
            status = "qualified" if blackboard.review_passed else "rejected"
            now = datetime.now(timezone.utc).isoformat()
            pipeline_log_json = json.dumps(
                blackboard.agent_log,
                ensure_ascii=False,
                default=str,
            )

            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO leads
                        (email_hash, company_domain, score, status,
                         contacted_at, became_customer, pipeline_log)
                    VALUES (?, ?, ?, ?, ?, FALSE, ?)
                    """,
                    (
                        email_hash,
                        company_domain,
                        blackboard.score,
                        status,
                        now,
                        pipeline_log_json,
                    ),
                )

            logger.info(
                "MemoryDB: Lead gespeichert fuer lead_id=%s, score=%d.",
                blackboard.lead_id,
                blackboard.score,
            )

        except Exception as exc:
            logger.error(
                "MemoryDB: save_lead fehlgeschlagen fuer lead_id=%s: %s",
                blackboard.lead_id,
                type(exc).__name__,
                exc_info=True,
            )

    def get_lead_history(self, email_hash: str) -> list[dict]:
        """
        Ruft die Lead-History fuer eine gehashte E-Mail-Adresse ab.

        Ermoeglicht es zu pruefen ob ein Kontakt bereits frueher als Lead
        qualifiziert oder abgelehnt wurde, ohne die echte E-Mail zu kennen.

        Args:
            email_hash: SHA-256-Hash der E-Mail-Adresse (aus hash_email()).

        Returns:
            Liste von Lead-Eintraegen als Dictionaries, neueste zuerst.
        """
        try:
            with self._get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT id, company_domain, score, status, contacted_at,
                           became_customer
                    FROM leads
                    WHERE email_hash = ?
                    ORDER BY contacted_at DESC
                    LIMIT 50
                    """,
                    (email_hash,),
                ).fetchall()

            return [dict(row) for row in rows]

        except Exception as exc:
            logger.error(
                "MemoryDB: get_lead_history fehlgeschlagen: %s",
                type(exc).__name__,
                exc_info=True,
            )
            return []

    def get_license(self, key: str) -> dict:
        """
        Holt Lizenzinformationen fuer einen gegebenen Key.

        Der Key wird gehasht bevor er in der Datenbank gesucht wird.
        Gibt ein leeres Dict zurueck wenn kein Eintrag gefunden wurde.

        Args:
            key: Klartex-Lizenzschluessel.

        Returns:
            Lizenz-Dictionary mit type, leads_used, created_at, metadata.
            Leeres Dict wenn Lizenz nicht gefunden.
        """
        try:
            key_hash = self._hash_key(key)

            with self._get_connection() as conn:
                # CAST created_at as TEXT um SQLite TIMESTAMP-Konvertierungsfehler zu vermeiden
                row = conn.execute(
                    """
                    SELECT key_hash, type, CAST(created_at AS TEXT) as created_at,
                           leads_used, metadata
                    FROM license
                    WHERE key_hash = ?
                    """,
                    (key_hash,),
                ).fetchone()

            if row is None:
                return {}

            result = dict(row)
            if isinstance(result.get("metadata"), str):
                try:
                    result["metadata"] = json.loads(result["metadata"])
                except (json.JSONDecodeError, TypeError):
                    result["metadata"] = {}

            return result

        except Exception as exc:
            logger.error(
                "MemoryDB: get_license fehlgeschlagen: %s",
                type(exc).__name__,
                exc_info=True,
            )
            return {}

    def save_license(
        self,
        key: str,
        license_type: str,
        metadata: Optional[dict] = None,
    ) -> None:
        """
        Speichert einen Lizenz-Eintrag in der Datenbank.

        Der Klartex-Key wird als SHA-256-Hash gespeichert.

        Args:
            key: Klartex-Lizenzschluessel.
            license_type: Typ der Lizenz (demo, starter, professional, enterprise).
            metadata: Optionale zusaetzliche Lizenzdaten als Dictionary.
        """
        try:
            key_hash = self._hash_key(key)
            now = datetime.now(timezone.utc).isoformat()
            metadata_json = json.dumps(metadata or {}, ensure_ascii=False)

            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO license
                        (key_hash, type, created_at, leads_used, metadata)
                    VALUES (?, ?, ?, 0, ?)
                    """,
                    (key_hash, license_type, now, metadata_json),
                )

            logger.info("MemoryDB: Lizenz gespeichert (Typ: %s).", license_type)

        except Exception as exc:
            logger.error(
                "MemoryDB: save_license fehlgeschlagen: %s",
                type(exc).__name__,
                exc_info=True,
            )

    def increment_lead_count(self, key: str) -> None:
        """
        Erhoeh den Zaehler fuer verarbeitete Leads um 1.

        Wird nach jeder erfolgreichen Lead-Verarbeitung aufgerufen.
        Erstellt einen Demo-Eintrag falls noch keiner existiert.

        Args:
            key: Klartex-Lizenzschluessel oder 'demo' fuer Demo-Modus.
        """
        try:
            key_hash = self._hash_key(key)

            with self._get_connection() as conn:
                # Pruefen ob Eintrag existiert
                row = conn.execute(
                    "SELECT leads_used FROM license WHERE key_hash = ?",
                    (key_hash,),
                ).fetchone()

                if row is None:
                    # Demo-Eintrag erstellen
                    now = datetime.now(timezone.utc).isoformat()
                    conn.execute(
                        """
                        INSERT INTO license (key_hash, type, created_at, leads_used, metadata)
                        VALUES (?, 'demo', ?, 1, '{}')
                        """,
                        (key_hash, now),
                    )
                    logger.debug(
                        "MemoryDB: Neuer Demo-Lizenz-Eintrag erstellt mit leads_used=1."
                    )
                else:
                    conn.execute(
                        """
                        UPDATE license
                        SET leads_used = leads_used + 1
                        WHERE key_hash = ?
                        """,
                        (key_hash,),
                    )
                    logger.debug(
                        "MemoryDB: leads_used inkrementiert auf %d.",
                        row["leads_used"] + 1,
                    )

        except Exception as exc:
            logger.error(
                "MemoryDB: increment_lead_count fehlgeschlagen: %s",
                type(exc).__name__,
                exc_info=True,
            )

    def get_leads_used(self, key: str) -> int:
        """
        Gibt die Anzahl bereits verarbeiteter Leads fuer einen Lizenzkey zurueck.

        Args:
            key: Klartex-Lizenzschluessel oder 'demo'.

        Returns:
            Anzahl verarbeiteter Leads (0 wenn noch kein Eintrag).
        """
        try:
            key_hash = self._hash_key(key)

            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT leads_used FROM license WHERE key_hash = ?",
                    (key_hash,),
                ).fetchone()

            if row is None:
                return 0
            return int(row["leads_used"] or 0)

        except Exception as exc:
            logger.error(
                "MemoryDB: get_leads_used fehlgeschlagen: %s",
                type(exc).__name__,
                exc_info=True,
            )
            return 0

    def get_license_created_at(self, key: str) -> Optional[datetime]:
        """
        Gibt den Erstellungszeitpunkt einer Lizenz zurueck.

        Args:
            key: Klartex-Lizenzschluessel oder 'demo'.

        Returns:
            Erstellungszeitpunkt als datetime oder None.
        """
        try:
            key_hash = self._hash_key(key)

            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT created_at FROM license WHERE key_hash = ?",
                    (key_hash,),
                ).fetchone()

            if row is None or not row["created_at"]:
                return None

            dt = datetime.fromisoformat(row["created_at"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        except Exception as exc:
            logger.error(
                "MemoryDB: get_license_created_at fehlgeschlagen: %s",
                type(exc).__name__,
                exc_info=True,
            )
            return None

    def save_scoring_feedback(
        self,
        lead_id: int,
        predicted_score: int,
        actual_outcome: str,
    ) -> None:
        """
        Speichert Feedback zum Scoring fuer spaetere Analyse.

        Wird verwendet um den Scoring-Algorithmus zu verbessern,
        indem reale Ergebnisse mit vorhergesagten Scores verglichen werden.

        Args:
            lead_id: Datenbank-ID des Leads.
            predicted_score: Urspruenglich vorhergesagter Score.
            actual_outcome: Tatsaechliches Ergebnis (z.B. "customer", "no_response").
        """
        try:
            now = datetime.now(timezone.utc).isoformat()

            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO scoring_feedback
                        (lead_id, predicted_score, actual_outcome, feedback_date)
                    VALUES (?, ?, ?, ?)
                    """,
                    (lead_id, predicted_score, actual_outcome, now),
                )

            logger.debug(
                "MemoryDB: Scoring-Feedback gespeichert fuer lead_id=%d.", lead_id
            )

        except Exception as exc:
            logger.error(
                "MemoryDB: save_scoring_feedback fehlgeschlagen: %s",
                type(exc).__name__,
                exc_info=True,
            )

    def hash_email(self, email: str) -> str:
        """
        Erstellt einen SHA-256-Hash einer E-Mail-Adresse.

        Verwendet lowercase-Normalisierung vor dem Hashing fuer konsistente
        Ergebnisse unabhaengig von der Schreibweise.
        Die E-Mail-Adresse selbst wird weder geloggt noch gespeichert.

        Args:
            email: E-Mail-Adresse im Klartext (wird NICHT geloggt).

        Returns:
            64-stelliger SHA-256-Hash als Hex-String.
        """
        normalized = email.strip().lower()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _hash_key(self, key: str) -> str:
        """
        Erstellt einen SHA-256-Hash eines Lizenzschluessels.

        Args:
            key: Klartex-Lizenzschluessel.

        Returns:
            SHA-256-Hash als Hex-String.
        """
        return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()

    def _extract_domain(self, url: str) -> str:
        """
        Extrahiert die Domain aus einer URL.

        Args:
            url: Vollstaendige URL (z.B. "https://www.beispiel.de/about").

        Returns:
            Domain ohne www-Prefix (z.B. "beispiel.de") oder leerer String.
        """
        if not url:
            return ""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            netloc = parsed.netloc.lower()
            # www. Prefix entfernen
            if netloc.startswith("www."):
                netloc = netloc[4:]
            return netloc
        except Exception:
            return ""
