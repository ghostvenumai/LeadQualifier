"""
WebScraper: Asynchrones Website-Scraping fuer den Research Agent.

Scrapt Unternehmenswebsites und extrahiert relevante Informationen
wie Titel, Beschreibung, Textinhalt, Links und Meta-Tags.

DSGVO-Hinweis:
    Keine personenbezogenen Daten werden extrahiert oder gespeichert.
    Nur oeffentlich zugaengliche Unternehmensinformationen werden verarbeitet.
"""

import logging
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Keywords fuer Industrie-Erkennung
_INDUSTRY_KEYWORDS: dict[str, list[str]] = {
    "IT": [
        "software", "saas", "cloud", "api", "entwicklung", "development",
        "programming", "coding", "devops", "kubernetes", "docker", "aws",
        "azure", "gcp", "microservices", "agile", "scrum", "tech", "digital",
        "plattform", "platform", "daten", "data", "ki", "ai", "ml",
    ],
    "Finance": [
        "fintech", "banking", "finance", "investment", "asset", "wealth",
        "insurance", "versicherung", "bank", "kredit", "finanzierung",
        "buchhaltung", "accounting", "steuer", "tax",
    ],
    "Healthcare": [
        "health", "medical", "medizin", "klinik", "krankenhaus", "pharma",
        "biotech", "diagnostik", "therapie", "pflege", "gesundheit",
    ],
    "Manufacturing": [
        "produktion", "fertigung", "manufacturing", "industrie", "maschinen",
        "anlagen", "automation", "robotik", "maschinenbau", "werkzeug",
    ],
    "Consulting": [
        "beratung", "consulting", "strategie", "management", "unternehmensberatung",
        "advisory", "transformation",
    ],
    "E-Commerce": [
        "shop", "store", "ecommerce", "e-commerce", "handel", "retail",
        "onlineshop", "marketplace", "versand",
    ],
    "Marketing": [
        "marketing", "werbung", "branding", "seo", "social media", "advertising",
        "pr", "kommunikation", "agentur", "design", "kreativ",
    ],
    "Legal": [
        "recht", "rechtsanwalt", "kanzlei", "law", "legal", "anwalt",
        "notar", "compliance",
    ],
    "Real Estate": [
        "immobilien", "real estate", "property", "bautraeger", "makler",
        "gewerbeimmobilien",
    ],
    "Education": [
        "bildung", "schule", "university", "university", "weiterbildung",
        "training", "e-learning", "lernplattform",
    ],
}

# Keywords fuer Unternehmensgroesse
_SIZE_KEYWORDS: dict[str, list[str]] = {
    "1-10": ["startup", "gruender", "freelance", "solopreneur", "einzelunternehmen"],
    "10-50": ["kleines unternehmen", "small business", "boutique", "kmu"],
    "50-200": ["mittelstand", "mittelstaendisch", "wachstum", "expanding"],
    "200-1000": ["enterprise", "konzern", "standorte", "niederlassungen"],
    "1000+": ["global", "international", "weltweit", "dax", "mdax"],
}

# Bekannte Tech-Stack-Indikatoren
_TECH_KEYWORDS: list[str] = [
    "react", "angular", "vue", "node", "python", "java", "php", "ruby",
    "django", "laravel", "spring", "dotnet", ".net", "wordpress", "shopify",
    "salesforce", "hubspot", "sap", "oracle", "microsoft", "google analytics",
    "gtm", "segment", "intercom", "zendesk", "jira", "confluence", "slack",
    "stripe", "paypal", "twilio", "sendgrid", "aws", "azure", "gcp",
    "kubernetes", "docker", "terraform", "jenkins", "gitlab", "github",
]


class WebScraper:
    """
    Asynchroner Website-Scraper fuer Unternehmensrecherche.

    Verwendet httpx.AsyncClient fuer nicht-blockierende HTTP-Requests
    und BeautifulSoup4 fuer HTML-Parsing.

    Attributes:
        _user_agent: User-Agent-Header fuer alle Requests.
        _timeout: Request-Timeout in Sekunden.
        _max_redirects: Maximale Anzahl erlaubter Redirects.
    """

    _user_agent: str = "LeadQualifier/1.0"
    _timeout: float = 10.0
    _max_redirects: int = 2

    async def scrape(self, url: str) -> dict:
        """
        Scrapt eine URL und gibt strukturierte Seitendaten zurueck.

        Extrahiert Titel, Beschreibung, Textinhalt, interne/externe Links
        und Meta-Tags. Bei Fehlern wird ein leeres Dictionary zurueckgegeben,
        damit die Pipeline nicht unterbrochen wird.

        Args:
            url: Die zu scrapende URL (muss http:// oder https:// enthalten).

        Returns:
            Dictionary mit Schluesseln: title, description, text_content,
            links, meta_tags. Leeres Dict bei Fehler.
        """
        if not url or not url.startswith(("http://", "https://")):
            logger.warning("WebScraper: Ungueltiges URL-Format: Schema fehlt.")
            return {}

        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": self._user_agent},
                timeout=httpx.Timeout(self._timeout),
                max_redirects=self._max_redirects,
                follow_redirects=True,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type and "text/plain" not in content_type:
                logger.debug(
                    "WebScraper: Unerwarteter Content-Type '%s' fuer URL.",
                    content_type,
                )
                return {}

            html = response.text
            return self._parse_html(html, url)

        except httpx.TimeoutException:
            logger.warning("WebScraper: Timeout beim Scrapen (URL nicht geloggt).")
            return {}
        except httpx.TooManyRedirects:
            logger.warning("WebScraper: Zu viele Redirects (max=%d).", self._max_redirects)
            return {}
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "WebScraper: HTTP-Fehler %d beim Scrapen.",
                exc.response.status_code,
            )
            return {}
        except httpx.RequestError as exc:
            logger.warning(
                "WebScraper: Netzwerkfehler beim Scrapen: %s",
                type(exc).__name__,
            )
            return {}
        except Exception as exc:
            logger.error(
                "WebScraper: Unerwarteter Fehler: %s",
                type(exc).__name__,
                exc_info=True,
            )
            return {}

    def _parse_html(self, html: str, base_url: str) -> dict:
        """
        Parst HTML und extrahiert strukturierte Daten.

        Args:
            html: Roher HTML-String.
            base_url: Basis-URL fuer relative Link-Aufloesung.

        Returns:
            Dictionary mit title, description, text_content, links, meta_tags.
        """
        try:
            soup = BeautifulSoup(html, "html.parser")

            # Skripte und Styles entfernen fuer cleanen Text
            for tag in soup(["script", "style", "noscript", "svg", "img"]):
                tag.decompose()

            title = self._extract_title(soup)
            description = self._extract_description(soup)
            text_content = self._extract_text(soup)
            links = self._extract_links(soup, base_url)
            meta_tags = self._extract_meta_tags(soup)

            return {
                "title": title,
                "description": description,
                "text_content": text_content[:5000],  # Auf 5000 Zeichen begrenzen
                "links": links[:50],                  # Maximal 50 Links
                "meta_tags": meta_tags,
            }
        except Exception as exc:
            logger.error(
                "WebScraper: HTML-Parsing-Fehler: %s",
                type(exc).__name__,
                exc_info=True,
            )
            return {}

    def _extract_title(self, soup: BeautifulSoup) -> str:
        """
        Extrahiert den Seitentitel.

        Versucht zuerst <title>, dann og:title, dann h1.

        Args:
            soup: BeautifulSoup-Objekt.

        Returns:
            Seitentitel als String oder leerer String.
        """
        # Standard <title>
        if soup.title and soup.title.string:
            return soup.title.string.strip()[:200]

        # Open Graph Title
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            return str(og_title["content"]).strip()[:200]

        # Erster H1
        h1 = soup.find("h1")
        if h1:
            return h1.get_text(strip=True)[:200]

        return ""

    def _extract_description(self, soup: BeautifulSoup) -> str:
        """
        Extrahiert die Seitenbeschreibung aus Meta-Tags.

        Args:
            soup: BeautifulSoup-Objekt.

        Returns:
            Beschreibung als String oder leerer String.
        """
        # Standard meta description
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            return str(meta_desc["content"]).strip()[:500]

        # Open Graph Description
        og_desc = soup.find("meta", property="og:description")
        if og_desc and og_desc.get("content"):
            return str(og_desc["content"]).strip()[:500]

        return ""

    def _extract_text(self, soup: BeautifulSoup) -> str:
        """
        Extrahiert bereinigten Textinhalt der Seite.

        Args:
            soup: BeautifulSoup-Objekt (Skripte bereits entfernt).

        Returns:
            Bereinigter Textinhalt.
        """
        text = soup.get_text(separator=" ", strip=True)
        # Mehrfache Whitespaces normalisieren
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _extract_links(self, soup: BeautifulSoup, base_url: str) -> list[dict]:
        """
        Extrahiert alle Links von der Seite.

        Args:
            soup: BeautifulSoup-Objekt.
            base_url: Basis-URL fuer relative Link-Aufloesung.

        Returns:
            Liste von Dictionaries mit href und text.
        """
        links: list[dict] = []
        parsed_base = urlparse(base_url)

        for a_tag in soup.find_all("a", href=True):
            href = str(a_tag["href"]).strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue

            # Relative URLs aufloesen
            if not href.startswith(("http://", "https://")):
                href = urljoin(base_url, href)

            parsed_href = urlparse(href)
            is_internal = parsed_href.netloc == parsed_base.netloc

            links.append({
                "href": href[:500],
                "text": a_tag.get_text(strip=True)[:100],
                "internal": is_internal,
            })

        return links

    def _extract_meta_tags(self, soup: BeautifulSoup) -> dict[str, str]:
        """
        Extrahiert alle relevanten Meta-Tags.

        Args:
            soup: BeautifulSoup-Objekt.

        Returns:
            Dictionary mit Meta-Tag-Namen als Schluessel.
        """
        meta_tags: dict[str, str] = {}
        relevant_names = {
            "description", "keywords", "author", "robots",
            "og:title", "og:description", "og:site_name", "og:type",
            "twitter:title", "twitter:description",
        }

        for meta in soup.find_all("meta"):
            name = meta.get("name") or meta.get("property") or ""
            content = meta.get("content", "")
            if name and content and name.lower() in relevant_names:
                meta_tags[name.lower()] = str(content).strip()[:300]

        return meta_tags

    def extract_company_info(self, html: str, domain: str) -> dict:
        """
        Extrahiert firmenspezifische Informationen aus dem HTML-Inhalt.

        Analysiert den Text auf Hinweise zu Branche, Unternehmensgroesse
        und verwendetem Tech-Stack. Keine KI-Analyse, nur Keyword-Matching.

        Args:
            html: Roher HTML-String der Unternehmenswebsite.
            domain: Domain des Unternehmens (fuer Kontext).

        Returns:
            Dictionary mit industry, size_hint, tech_hints und keywords_found.
        """
        try:
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()

            text = soup.get_text(separator=" ", strip=True).lower()

            industry = self._detect_industry(text)
            size_hint = self._detect_size(text)
            tech_hints = self._detect_tech_stack(text, html.lower())

            return {
                "industry": industry,
                "size_hint": size_hint,
                "tech_hints": tech_hints,
                "has_impressum": "impressum" in text,
                "has_datenschutz": "datenschutz" in text or "privacy" in text,
                "has_agb": "agb" in text or "allgemeine geschaeftsbedingungen" in text,
                "is_dach_region": self._detect_dach(text),
                "domain": domain,
            }
        except Exception as exc:
            logger.error(
                "WebScraper: extract_company_info fehlgeschlagen fuer Domain=%s: %s",
                domain,
                type(exc).__name__,
                exc_info=True,
            )
            return {
                "industry": "unknown",
                "size_hint": "unknown",
                "tech_hints": [],
                "has_impressum": False,
                "has_datenschutz": False,
                "has_agb": False,
                "is_dach_region": False,
                "domain": domain,
            }

    def _detect_industry(self, text_lower: str) -> str:
        """
        Erkennt die Branche anhand von Schluesselwoertern im Text.

        Args:
            text_lower: Kleingeschriebener Textinhalt der Website.

        Returns:
            Erkannte Branche oder 'unknown'.
        """
        scores: dict[str, int] = {}

        for industry, keywords in _INDUSTRY_KEYWORDS.items():
            count = sum(1 for kw in keywords if kw in text_lower)
            if count > 0:
                scores[industry] = count

        if not scores:
            return "unknown"

        return max(scores, key=lambda k: scores[k])

    def _detect_size(self, text_lower: str) -> str:
        """
        Erkennt Groessenhinweise fuer das Unternehmen.

        Sucht nach numerischen Angaben (z.B. "150 Mitarbeiter") und
        Schluesselwoertern.

        Args:
            text_lower: Kleingeschriebener Textinhalt.

        Returns:
            Geschaetzte Groessenrange oder 'unknown'.
        """
        # Direkte Mitarbeiterzahl-Erkennung (z.B. "120 Mitarbeiter", "50 employees")
        patterns = [
            r"(\d+)\s*(?:\+)?\s*mitarbeiter",
            r"(\d+)\s*(?:\+)?\s*employees",
            r"(\d+)\s*(?:\+)?\s*angestellte",
            r"team\s+(?:von|of)\s+(\d+)",
            r"(\d+)\s*(?:\+)?\s*personen",
        ]

        for pattern in patterns:
            match = re.search(pattern, text_lower)
            if match:
                try:
                    count = int(match.group(1))
                    if count <= 10:
                        return "1-10"
                    elif count <= 50:
                        return "10-50"
                    elif count <= 200:
                        return "50-200"
                    elif count <= 1000:
                        return "200-1000"
                    else:
                        return "1000+"
                except ValueError:
                    pass

        # Keyword-basierte Erkennung
        for size_range, keywords in _SIZE_KEYWORDS.items():
            if any(kw in text_lower for kw in keywords):
                return size_range

        return "unknown"

    def _detect_tech_stack(self, text_lower: str, html_lower: str) -> list[str]:
        """
        Erkennt verwendete Technologien aus Text und HTML-Quellcode.

        Args:
            text_lower: Kleingeschriebener sichtbarer Text.
            html_lower: Kleingeschriebener HTML-Quellcode.

        Returns:
            Liste erkannter Technologien.
        """
        found: list[str] = []

        for tech in _TECH_KEYWORDS:
            # Suche in Text und HTML (fuer Script-Tags, Kommentare etc.)
            if tech in text_lower or tech in html_lower:
                found.append(tech)

        # Deduplizieren und sortieren
        return sorted(set(found))[:15]

    # Gaengige Pfade fuer Karriere-Seiten auf Unternehmenswebsites
    _JOB_PAGE_PATHS: list[str] = [
        "/jobs", "/karriere", "/stellenangebote", "/careers",
        "/arbeiten-bei-uns", "/stellen", "/offene-stellen",
        "/stellenmarkt", "/job-angebote", "/en/jobs", "/en/careers",
        "/jobangebote", "/freie-stellen", "/work-with-us",
    ]

    # Keywords die auf aktive Stellenangebote hinweisen
    _HIRING_KEYWORDS: frozenset[str] = frozenset({
        "wir suchen", "stellenangebot", "stellenangebote", "offene stellen",
        "freie stellen", "jetzt bewerben", "bewerbung einreichen",
        "verstärke unser team", "team verstärken", "werde teil",
        "wir freuen uns auf", "initiativbewerbung",
        "we are hiring", "we're hiring", "join our team", "now hiring",
        "open position", "open positions", "job opening", "apply now",
        "apply here", "submit application", "apply today",
    })

    async def check_hiring_status(self, base_url: str) -> dict:
        """
        Prueft ob ein Unternehmen aktuell Mitarbeiter sucht.

        Versucht gaengige Karriere-Seiten-Pfade zu erreichen und analysiert
        den Inhalt auf aktive Stellenangebote.

        Args:
            base_url: Basis-URL des Unternehmens (z.B. "https://example.de").

        Returns:
            Dictionary mit is_hiring (bool|None), job_page_url (str|None)
            und open_positions_count (int).
        """
        result: dict = {
            "is_hiring": None,
            "job_page_url": None,
            "open_positions_count": 0,
        }

        if not base_url:
            return result

        base_url = base_url.rstrip("/")

        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": self._user_agent},
                timeout=httpx.Timeout(self._timeout),
                max_redirects=self._max_redirects,
                follow_redirects=True,
            ) as client:
                for path in self._JOB_PAGE_PATHS:
                    job_url = f"{base_url}{path}"
                    try:
                        response = await client.get(job_url)
                    except (httpx.TimeoutException, httpx.RequestError):
                        continue

                    if response.status_code != 200:
                        continue
                    if "text/html" not in response.headers.get("content-type", ""):
                        continue

                    text_lower = response.text.lower()
                    found = [kw for kw in self._HIRING_KEYWORDS if kw in text_lower]

                    if found:
                        soup = BeautifulSoup(response.text, "html.parser")
                        count = self._count_job_listings(soup)
                        result["is_hiring"] = True
                        result["job_page_url"] = str(response.url)
                        result["open_positions_count"] = count
                        logger.debug(
                            "WebScraper: Karriere-Seite gefunden, %d Stellen erkannt.",
                            count,
                        )
                        return result

                    # Seite existiert, aber keine aktiven Anzeigen
                    result["is_hiring"] = False
                    result["job_page_url"] = str(response.url)
                    return result

        except Exception as exc:
            logger.debug("WebScraper: Fehler bei Hiring-Check: %s", type(exc).__name__)

        return result

    def _count_job_listings(self, soup: BeautifulSoup) -> int:
        """
        Zaehlt potenzielle Stellenanzeigen auf einer Karriere-Seite.

        Sucht nach gaengigen HTML-Strukturen fuer Job-Listings
        (article, li, div mit job-bezogenen CSS-Klassen).

        Args:
            soup: BeautifulSoup-Objekt der Karriere-Seite.

        Returns:
            Geschaetzte Anzahl offener Stellen (Minimum 1 wenn Seite aktiv ist).
        """
        count = 0
        job_class_keywords = {"job", "stelle", "position", "career", "vacancy", "opening"}

        for tag in soup.find_all(["article", "li", "div"], limit=200):
            classes = " ".join(tag.get("class", [])).lower()
            if any(kw in classes for kw in job_class_keywords):
                count += 1

        return max(count, 1)

    def _detect_dach(self, text_lower: str) -> bool:
        """
        Prueft ob das Unternehmen in der DACH-Region ansaessig ist.

        Args:
            text_lower: Kleingeschriebener Textinhalt.

        Returns:
            True wenn DACH-Indikatoren gefunden wurden.
        """
        dach_indicators = [
            "deutschland", "oesterreich", "schweiz", "germany", "austria",
            "switzerland", " gmbh", " ag ", "ug ", "e.v.", "kg ",
            ".de", ".at", ".ch", "mwst", "mehrwertsteuer", "ust-id",
        ]
        return any(indicator in text_lower for indicator in dach_indicators)
