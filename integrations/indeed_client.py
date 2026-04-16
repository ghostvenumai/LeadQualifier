"""
IndeedJobChecker: Prueft ob ein Unternehmen auf Indeed aktuelle Stellenanzeigen hat.

Scrapt die oeffentliche Indeed-Jobsuche nach dem Firmennamen um festzustellen
ob ein Unternehmen aktuell Mitarbeiter sucht. Dient als Ergaenzung zur
Website-Analyse des ResearchAgents.

DSGVO-Hinweis:
    Nur oeffentlich zugaengliche Unternehmensinformationen werden abgerufen.
    Keine personenbezogenen Bewerber-Daten werden verarbeitet oder gespeichert.
    Firmennamen werden nicht geloggt.
"""

import logging
import re
from typing import Optional
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_INDEED_BASE_URL: str = "https://de.indeed.com"
_HTTP_TIMEOUT: float = 8.0
_HTTP_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
}

# Rechtliche Unternehmenszusaetze die vor der Suche entfernt werden
_LEGAL_SUFFIXES: list[str] = [
    " GmbH", " AG", " UG", " KG", " OHG", " GbR",
    " & Co.", " & Co", " Co.", " Ltd", " Inc", " Corp",
    " e.K.", " SE", " mbH",
]


class IndeedJobChecker:
    """
    Sucht auf Indeed nach offenen Stellen eines Unternehmens.

    Verwendet die oeffentliche Indeed-Jobsuche (de.indeed.com) um zu
    ermitteln ob ein Unternehmen aktuell aktiv Mitarbeiter rekrutiert.
    Wachstum und Stellenausschreibungen sind ein starkes Budget-Signal.

    Bei Fehlern (Netzwerk, Bot-Detection, Parsing-Fehler) wird ein
    leeres Ergebnis zurueckgegeben — die Pipeline crasht nicht.
    """

    async def search_company_jobs(
        self,
        company_name: str,
        location: str = "Deutschland",
    ) -> dict:
        """
        Sucht auf Indeed nach aktuellen Stellen des Unternehmens.

        Args:
            company_name: Name des Unternehmens (z.B. "Musterfirma GmbH").
            location: Suchregion (default: "Deutschland").

        Returns:
            Dictionary mit:
                is_hiring (bool|None): True wenn Stellen gefunden.
                open_positions_count (int): Anzahl gefundener Stellen.
                job_titles (list[str]): Liste erkannter Jobtitel (max. 10).
                source_url (str|None): Verwendete Such-URL.
        """
        result: dict = {
            "is_hiring": None,
            "open_positions_count": 0,
            "job_titles": [],
            "source_url": None,
        }

        if not company_name or not company_name.strip():
            return result

        clean_name = self._clean_company_name(company_name)
        if not clean_name:
            return result

        search_url = (
            f"{_INDEED_BASE_URL}/jobs"
            f"?q=%22{quote(clean_name)}%22"
            f"&l={quote(location)}"
            f"&sort=date"
        )
        result["source_url"] = search_url

        try:
            async with httpx.AsyncClient(
                headers=_HTTP_HEADERS,
                timeout=httpx.Timeout(_HTTP_TIMEOUT),
                max_redirects=2,
                follow_redirects=True,
            ) as client:
                response = await client.get(search_url)

            if response.status_code != 200:
                logger.debug(
                    "IndeedJobChecker: HTTP %d empfangen.",
                    response.status_code,
                )
                return result

            # Bot-Detection erkennen
            if "captcha" in response.text.lower() or "sind sie ein mensch" in response.text.lower():
                logger.debug("IndeedJobChecker: Bot-Detection ausgeloest, ueberspringe.")
                return result

            count, titles = self._parse_search_results(response.text)

            if count > 0:
                result["is_hiring"] = True
                result["open_positions_count"] = count
                result["job_titles"] = titles
            else:
                result["is_hiring"] = False

            logger.debug(
                "IndeedJobChecker: %d Stellen gefunden.",
                count,
            )

        except httpx.TimeoutException:
            logger.debug("IndeedJobChecker: Timeout bei der Anfrage.")
        except httpx.RequestError as exc:
            logger.debug("IndeedJobChecker: Netzwerkfehler: %s", type(exc).__name__)
        except Exception as exc:
            logger.debug("IndeedJobChecker: Unerwarteter Fehler: %s", type(exc).__name__)

        return result

    def _clean_company_name(self, name: str) -> str:
        """
        Entfernt rechtliche Zusaetze aus dem Firmennamen fuer bessere Suchtreffer.

        Args:
            name: Roher Firmenname (z.B. "Musterfirma GmbH & Co. KG").

        Returns:
            Bereinigter Name fuer die Suche (z.B. "Musterfirma").
        """
        clean = name.strip()
        for suffix in _LEGAL_SUFFIXES:
            # Case-insensitive Entfernung
            clean = re.sub(re.escape(suffix), "", clean, flags=re.IGNORECASE)
        return clean.strip()

    def _parse_search_results(self, html: str) -> tuple[int, list[str]]:
        """
        Parst Indeed-Suchergebnisse nach Anzahl und Jobtiteln.

        Args:
            html: HTML-Inhalt der Indeed-Suchergebnisseite.

        Returns:
            Tupel aus (Anzahl_Stellen: int, Jobtitel: list[str]).
        """
        try:
            soup = BeautifulSoup(html, "html.parser")
            titles: list[str] = []

            # Ergebnis-Zaehler aus dem Header extrahieren
            # Indeed zeigt z.B. "37 Jobs in Deutschland" oder "Keine Ergebnisse"
            count = self._extract_result_count(soup)

            # Jobtitel aus den Suchergebniskarten sammeln
            # Indeed-Karten haben das Attribut data-jk
            job_cards = soup.find_all(attrs={"data-jk": True})
            if not job_cards:
                # Fallback fuer aeltere Indeed-Layouts
                job_cards = soup.select(".job_seen_beacon, .result, .tapItem")

            for card in job_cards[:15]:
                title = self._extract_job_title(card)
                if title:
                    titles.append(title)

            # Wenn count nicht aus Header ermittelbar: Kartenanzahl nutzen
            if count == 0 and titles:
                count = len(titles)

            return count, titles[:10]

        except Exception as exc:
            logger.debug(
                "IndeedJobChecker: Parsing-Fehler: %s", type(exc).__name__
            )
            return 0, []

    def _extract_result_count(self, soup: BeautifulSoup) -> int:
        """
        Extrahiert die Trefferzahl aus dem Indeed-Suchergebnis-Header.

        Args:
            soup: BeautifulSoup-Objekt der Suchergebnisseite.

        Returns:
            Anzahl gefundener Stellen (0 wenn nicht ermittelbar).
        """
        # Verschiedene Indeed-Layouts abdecken
        selectors = [
            "#searchCountPages",
            ".jobsearch-JobCountAndSortPane-jobCount",
            "[data-testid='searchCount']",
            ".jobCount",
        ]
        for selector in selectors:
            element = soup.select_one(selector)
            if element:
                text = element.get_text(strip=True)
                # Zahlen aus Text extrahieren (z.B. "1.234 Jobs" -> 1234)
                digits = re.sub(r"[^\d]", "", text.split(" ")[0])
                if digits:
                    return min(int(digits), 9999)

        return 0

    def _extract_job_title(self, card: BeautifulSoup) -> Optional[str]:
        """
        Extrahiert den Jobtitel aus einer Indeed-Ergebniskarte.

        Args:
            card: BeautifulSoup-Tag einer Job-Ergebniskarte.

        Returns:
            Jobtitel als String oder None wenn nicht ermittelbar.
        """
        # Indeed-Jobtitel sind typischerweise in h2 > a oder span.title
        for selector in ["h2 a span", "h2 span", ".title", "a[data-jk]"]:
            element = card.select_one(selector)
            if element:
                text = element.get_text(strip=True)
                if text and 3 <= len(text) <= 150:
                    return text
        return None
