"""
ResearchAgent: Recherchiert Firmendaten fuer eingehende Leads.

Scrapt die Firmenwebsite, prueft die E-Mail-Domain auf Professionalitaet,
extrahiert Branche, Groesse und Firmenname, generiert eine LinkedIn-URL
und schreibt alle Ergebnisse in das LeadBlackboard.

Verwendete Bibliotheken:
    - httpx (async HTTP-Client, timeout=10, max 2 redirects)
    - beautifulsoup4 (HTML-Parsing)

DSGVO-Hinweis:
    Keine personenbezogenen Daten werden in Logs geschrieben.
    Die E-Mail-Adresse wird nur zur Domain-Pruefung verwendet, nicht geloggt.
"""

import logging
import re
from typing import Any, Optional

import httpx
from bs4 import BeautifulSoup

from core.blackboard import LeadBlackboard
from core.config import Settings
from core.models import ResearchData
from integrations.scraper import WebScraper
from integrations.indeed_client import IndeedJobChecker

logger = logging.getLogger(__name__)

# Kostenlose E-Mail-Domains die auf nicht-professionelle Kontakte hinweisen
_FREE_EMAIL_DOMAINS: frozenset[str] = frozenset({
    "gmail.com",
    "gmx.de",
    "gmx.net",
    "gmx.at",
    "gmx.ch",
    "web.de",
    "hotmail.com",
    "hotmail.de",
    "hotmail.at",
    "yahoo.com",
    "yahoo.de",
    "yahoo.at",
    "outlook.com",
    "outlook.de",
    "t-online.de",
    "freenet.de",
    "icloud.com",
    "me.com",
    "aol.com",
    "live.com",
    "live.de",
    "protonmail.com",
    "posteo.de",
    "mailbox.org",
})

# Keywords die auf Entscheidungstraeger hinweisen (Titel/Jobtitel)
_DECISION_MAKER_TITLES: frozenset[str] = frozenset({
    "ceo", "cto", "cfo", "coo", "cmo", "cso", "cpo",
    "geschaeftsfuehrer", "geschäftsführer", "inhaber", "eigentuemer", "eigentümer",
    "gruender", "gründer", "co-founder", "founder",
    "vorstand", "direktor", "director",
    "leiter", "head of", "vp", "vice president",
    "managing director", "md", "general manager",
    "president", "partner",
})

# Keywords die auf Studenten/Praktikanten hinweisen
_NON_DECISION_MAKER_TITLES: frozenset[str] = frozenset({
    "student", "studentin", "praktikant", "praktikantin",
    "werkstudent", "werkstudentin", "auszubildender", "azubi",
    "trainee", "intern", "bachelor", "master", "phd", "kandidat",
    "hochschule", "universität", "uni ", " uni", "fh ", " fh",
})

# Branchen-Keywords fuer die automatische Klassifikation
_INDUSTRY_KEYWORDS: dict[str, list[str]] = {
    "IT": [
        "software", "it-dienst", "technologie", "tech ", "digital",
        "cloud", "saas", "entwicklung", "programmierung", "data",
        "cyber", "security", "netzwerk", "hosting", "devops",
        "artificial intelligence", "machine learning", "ki-",
    ],
    "Marketing": [
        "marketing", "agentur", "werbung", "pr ", "kommunikation",
        "content", "seo", "social media", "branding", "design",
        "kampagne", "advertising", "media",
    ],
    "Beratung": [
        "beratung", "consulting", "unternehmensberatung", "strategie",
        "management", "advisory", "coach", "training", "academy",
    ],
    "Finance": [
        "finanz", "bank", "versicherung", "invest", "kapital",
        "steuer", "buchhaltung", "buchhaltungs", "wirtschaftsprüf",
        "wirtschaftsprüfer", "controlling",
    ],
    "Healthcare": [
        "gesundheit", "medizin", "pharma", "klinik", "krankenhaus",
        "arzt", "praxis", "therapeut", "medical",
    ],
    "E-Commerce": [
        "shop", "handel", "e-commerce", "online-shop", "marktplatz",
        "retail", "logistik", "fulfillment",
    ],
}

# HTTP-Konfiguration
_HTTP_TIMEOUT: int = 10
_HTTP_MAX_REDIRECTS: int = 2
_HTTP_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; LeadQualifier/1.0; "
        "+https://ghostvenumai.de/bot)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
}


class ResearchAgent:
    """
    Recherchiert Firmendaten fuer einen eingehenden Lead.

    Scrapt die Firmenwebsite (wenn ableitbar), prueft die E-Mail-Domain,
    klassifiziert Branche und Groesse und generiert eine LinkedIn-URL.
    Alle Ergebnisse werden in blackboard.research als Dictionary gespeichert,
    das der ResearchData-Struktur entspricht.

    Bei Fehlern (Netzwerk, Timeout, Parse-Fehler) wird ein leeres
    ResearchData zurueckgegeben - die Pipeline crasht nicht.
    """

    def __init__(
        self,
        blackboard: LeadBlackboard,
        settings: Settings,
        scraper: Optional[Any] = None,
        web_scraper: Optional[WebScraper] = None,
        indeed_checker: Optional[IndeedJobChecker] = None,
    ) -> None:
        """
        Initialisiert den ResearchAgent.

        Args:
            blackboard: Das gemeinsame Blackboard-Objekt der aktuellen Pipeline.
            settings: Konfigurationsobjekt.
            scraper: Optionaler externer HTTP-Scraper (fuer Tests injizierbar).
                     Wenn None, wird httpx.AsyncClient verwendet.
            web_scraper: Optionaler WebScraper fuer Karriereseiten-Check (DI fuer Tests).
                         Wenn None, wird eine neue WebScraper-Instanz erstellt.
            indeed_checker: Optionaler IndeedJobChecker (DI fuer Tests).
                            Wenn None, wird eine neue IndeedJobChecker-Instanz erstellt.
        """
        self._blackboard = blackboard
        self._settings = settings
        self._scraper = scraper  # Dependency Injection fuer Tests
        self._web_scraper = web_scraper or WebScraper()
        self._indeed_checker = indeed_checker or IndeedJobChecker()

    # ------------------------------------------------------------------
    # Oeffentliche Methode
    # ------------------------------------------------------------------

    async def research(self) -> ResearchData:
        """
        Fuehrt die vollstaendige Firmenrecherche durch.

        Ablauf:
            1. E-Mail-Domain auf Professionalitaet pruefen
            2. Website-URL aus Domain ableiten
            3. Website scrapen (Titel, Meta-Description, Text-Extrakte)
            4. Branche und Groesse klassifizieren
            5. LinkedIn-URL generieren
            6. Ergebnis in Blackboard schreiben

        Returns:
            ResearchData mit allen verfuegbaren Firmendaten.
        """
        logger.info(
            "ResearchAgent gestartet fuer lead_id=%s",
            self._blackboard.lead_id,
        )

        try:
            result = await self._run_research()
            self._blackboard.research = result.model_dump()
            self._blackboard.log("ResearchAgent", "research_complete")
            logger.info(
                "ResearchAgent abgeschlossen: lead_id=%s, industry=%s, size=%s",
                self._blackboard.lead_id,
                result.industry,
                result.size,
            )
            return result

        except Exception as exc:
            logger.warning(
                "ResearchAgent: Unerwarteter Fehler fuer lead_id=%s: %s",
                self._blackboard.lead_id,
                type(exc).__name__,
            )
            empty = ResearchData()
            self._blackboard.research = empty.model_dump()
            self._blackboard.log("ResearchAgent", f"research_failed: {type(exc).__name__}")
            return empty

    # ------------------------------------------------------------------
    # Interne Hilfsmethoden
    # ------------------------------------------------------------------

    async def _run_research(self) -> ResearchData:
        """
        Fuehrt die eigentliche Recherche-Logik aus.

        Returns:
            Befuelltes ResearchData-Objekt.
        """
        bb = self._blackboard

        # 1. E-Mail-Domain ermitteln (Basis fuer Website-Ableitung;
        #    die Professionell-Pruefung macht der ScoringAgent selbst)
        email_domain = self._extract_email_domain(bb.email)

        # 2. Website-URL ermitteln
        website_url = self._derive_website_url(email_domain, bb.company)

        # 3. Website scrapen
        scraped: dict[str, Any] = {}
        if website_url:
            scraped = await self._scrape_website(website_url)

        # 4. Entscheider-Erkennung anhand des Namens/der Nachricht
        is_decision_maker = self._detect_decision_maker(bb.name, bb.message)

        # 5. Branche klassifizieren
        industry = self._classify_industry(
            scraped.get("text_content", ""),
            bb.company,
            bb.message,
        )

        # 6. Unternehmensgroesse schaetzen
        size = self._estimate_company_size(
            scraped.get("text_content", ""),
            bb.message,
        )

        # 7. LinkedIn-URL generieren
        linkedin_url = self._generate_linkedin_url(bb.company)

        # 8. Website-Qualitaet bewerten
        website_quality = scraped.get("website_quality", "none" if not website_url else "unknown")

        # 9. Location aus Website-Content extrahieren
        location = self._extract_location(scraped.get("text_content", ""))

        # 10. Hiring-Status pruefen (Website-Karriereseite + Indeed)
        hiring = await self._check_hiring_status(website_url, bb.company)

        return ResearchData(
            industry=industry,
            size=size,
            is_decision_maker=is_decision_maker,
            website_quality=website_quality,
            linkedin_url=linkedin_url,
            website_url=website_url or None,
            news_items=scraped.get("news_items", []),
            cached=False,
            tech_stack=scraped.get("tech_stack", []),
            location=location,
            is_hiring=hiring["is_hiring"],
            open_positions_count=hiring["open_positions_count"],
            job_page_url=hiring["job_page_url"],
        )

    def _extract_email_domain(self, email: str) -> str:
        """
        Extrahiert die Domain aus einer E-Mail-Adresse.

        Args:
            email: E-Mail-Adresse (wird nicht geloggt).

        Returns:
            Lowercase-Domain-String oder leerer String bei ungueltigem Format.
        """
        if not email or "@" not in email:
            return ""
        return email.split("@")[-1].lower().strip()

    def _is_professional_domain(self, domain: str) -> bool:
        """
        Prueft ob eine E-Mail-Domain als professionell gilt.

        Args:
            domain: E-Mail-Domain (z.B. "example.de").

        Returns:
            True wenn die Domain nicht in der Liste kostenloser Domains ist.
        """
        if not domain:
            return False
        return domain not in _FREE_EMAIL_DOMAINS

    def _derive_website_url(self, email_domain: str, company_name: str) -> str:
        """
        Leitet eine wahrscheinliche Website-URL aus der E-Mail-Domain ab.

        Bei professionellen Domains wird direkt die Domain verwendet.
        Bei kostenlosen Domains wird versucht, aus dem Firmennamen einen
        URL-Slug zu generieren.

        Args:
            email_domain: Domain der E-Mail-Adresse.
            company_name: Firmenname aus dem Lead-Input.

        Returns:
            URL-String mit https:// Prefix oder leerer String.
        """
        if email_domain and email_domain not in _FREE_EMAIL_DOMAINS:
            return f"https://{email_domain}"

        # Fallback: Firmennamen als Slug versuchen
        if company_name:
            slug = self._company_to_slug(company_name)
            if slug:
                return f"https://www.{slug}.de"

        return ""

    def _company_to_slug(self, company_name: str) -> str:
        """
        Konvertiert einen Firmennamen in einen URL-kompatiblen Slug.

        Args:
            company_name: Roher Firmenname.

        Returns:
            URL-kompatibler Slug (lowercase, nur alphanumerisch und Bindestriche).
        """
        # Rechtliche Zusaetze entfernen
        for suffix in [" gmbh", " ag", " ug", " kg", " ohg", " gbr",
                        " & co", " co.", " ltd", " inc", " corp", " e.k."]:
            company_name = company_name.lower().replace(suffix, "")

        # Umlaute ersetzen
        replacements = {
            "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
            "Ä": "ae", "Ö": "oe", "Ü": "ue",
        }
        for char, replacement in replacements.items():
            company_name = company_name.replace(char, replacement)

        # Nur alphanumerische Zeichen und Leerzeichen behalten
        slug = re.sub(r"[^a-z0-9\s-]", "", company_name.lower())
        slug = re.sub(r"\s+", "-", slug.strip())
        slug = re.sub(r"-+", "-", slug).strip("-")

        return slug if len(slug) >= 2 else ""

    async def _scrape_website(self, url: str) -> dict[str, Any]:
        """
        Scrapt eine Website und extrahiert relevante Informationen.

        Verwendet httpx.AsyncClient mit timeout=10 und max. 2 Redirects.
        Bei Fehlern wird ein leeres Dictionary zurueckgegeben.

        Args:
            url: Vollstaendige URL der zu scrapenden Website.

        Returns:
            Dictionary mit text_content, website_quality, tech_stack, news_items.
        """
        try:
            client_kwargs: dict[str, Any] = {
                "timeout": httpx.Timeout(_HTTP_TIMEOUT),
                "max_redirects": _HTTP_MAX_REDIRECTS,
                "headers": _HTTP_HEADERS,
                "follow_redirects": True,
            }

            if self._scraper is not None:
                # Injizierter Test-Scraper
                response = await self._scraper.get(url)
            else:
                async with httpx.AsyncClient(**client_kwargs) as client:
                    response = await client.get(url)

            if response.status_code >= 400:
                logger.debug(
                    "ResearchAgent: Website lieferte Status %d fuer lead_id=%s",
                    response.status_code,
                    self._blackboard.lead_id,
                )
                return {"website_quality": "poor"}

            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type and "application/xhtml" not in content_type:
                return {"website_quality": "average"}

            html_content = response.text
            return self._parse_html(html_content, url)

        except httpx.TimeoutException:
            logger.debug(
                "ResearchAgent: Timeout beim Scrapen fuer lead_id=%s",
                self._blackboard.lead_id,
            )
            return {"website_quality": "unknown"}

        except httpx.TooManyRedirects:
            logger.debug(
                "ResearchAgent: Zu viele Redirects fuer lead_id=%s",
                self._blackboard.lead_id,
            )
            return {"website_quality": "poor"}

        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            logger.debug(
                "ResearchAgent: HTTP-Fehler fuer lead_id=%s: %s",
                self._blackboard.lead_id,
                type(exc).__name__,
            )
            return {"website_quality": "none"}

    def _parse_html(self, html: str, base_url: str) -> dict[str, Any]:
        """
        Parst HTML-Inhalt und extrahiert relevante Textinformationen.

        Args:
            html: Rohes HTML als String.
            base_url: Basis-URL fuer relative Links.

        Returns:
            Dictionary mit extrahierten Informationen.
        """
        try:
            soup = BeautifulSoup(html, "html.parser")

            # Script- und Style-Tags entfernen
            for tag in soup(["script", "style", "noscript", "iframe"]):
                tag.decompose()

            # Titel und Meta-Description
            title = soup.title.string.strip() if soup.title and soup.title.string else ""
            meta_desc = ""
            meta_tag = soup.find("meta", attrs={"name": "description"})
            if meta_tag and isinstance(meta_tag, type(soup.find("meta"))):
                meta_desc = meta_tag.get("content", "")  # type: ignore[union-attr]

            # Sichtbaren Text extrahieren (auf 3000 Zeichen begrenzen)
            body = soup.find("body")
            raw_text = body.get_text(separator=" ", strip=True) if body else ""
            text_content = f"{title} {meta_desc} {raw_text}"[:3000]

            # Tech-Stack aus Meta-Tags und HTML-Attributen erkennen
            tech_stack = self._detect_tech_stack(soup, html)

            # Website-Qualitaet bewerten
            website_quality = self._assess_website_quality(soup, title, meta_desc, raw_text)

            return {
                "text_content": text_content,
                "website_quality": website_quality,
                "tech_stack": tech_stack,
                "news_items": [],  # News-Extraktion erfordert tieferes Parsing
            }

        except Exception as exc:
            logger.debug(
                "ResearchAgent: HTML-Parsing-Fehler fuer lead_id=%s: %s",
                self._blackboard.lead_id,
                type(exc).__name__,
            )
            return {"website_quality": "unknown", "text_content": ""}

    def _detect_tech_stack(self, soup: BeautifulSoup, raw_html: str) -> list[str]:
        """
        Erkennt verwendete Technologien anhand von HTML-Signaturen.

        Args:
            soup: BeautifulSoup-Objekt des geparsten HTML.
            raw_html: Rohes HTML als String fuer Regex-Suche.

        Returns:
            Liste erkannter Technologien.
        """
        tech_stack: list[str] = []
        html_lower = raw_html.lower()

        tech_signatures: dict[str, list[str]] = {
            "WordPress": ["wp-content", "wp-includes", "wordpress"],
            "Shopify": ["shopify", "cdn.shopify.com"],
            "HubSpot": ["hubspot", "hs-scripts"],
            "Salesforce": ["salesforce", "force.com"],
            "Google Analytics": ["google-analytics", "gtag(", "ua-"],
            "React": ["react", "__reactfiber", "react-dom"],
            "Vue.js": ["vue.js", "__vue__", "vuejs"],
            "Angular": ["ng-version", "angular"],
            "Matomo": ["matomo", "_paq"],
            "Intercom": ["intercom"],
            "Zendesk": ["zopim", "zendesk"],
            "SAP": ["sap.com", "sapui5"],
            "Microsoft 365": ["microsoftonline", "sharepoint"],
        }

        for tech, signatures in tech_signatures.items():
            if any(sig in html_lower for sig in signatures):
                tech_stack.append(tech)

        return tech_stack[:8]  # Maximal 8 Eintraege

    def _assess_website_quality(
        self,
        soup: BeautifulSoup,
        title: str,
        meta_desc: str,
        body_text: str,
    ) -> str:
        """
        Bewertet die Qualitaet einer Website anhand einfacher Heuristiken.

        Args:
            soup: Geparster HTML-Inhalt.
            title: Seitentitel.
            meta_desc: Meta-Description.
            body_text: Sichtbarer Seitentext.

        Returns:
            Qualitaetsbewertung: "professional", "average" oder "poor".
        """
        score = 0

        if title and len(title) > 5:
            score += 1
        if meta_desc and len(meta_desc) > 20:
            score += 1
        if len(body_text) > 500:
            score += 1
        if soup.find("nav") or soup.find("header"):
            score += 1
        if soup.find("footer"):
            score += 1
        # SSL wird durch httpx implizit geprueft (https://)
        score += 1  # Wenn wir bis hierher kommen, ist https erreichbar

        if score >= 5:
            return "professional"
        elif score >= 3:
            return "average"
        else:
            return "poor"

    def _detect_decision_maker(self, name: str, message: str) -> bool:
        """
        Erkennt ob ein Kontakt wahrscheinlich Entscheidungstraeger ist.

        Prueft den Namen und die Nachricht auf Hinweise auf Entscheider-
        oder Nicht-Entscheider-Titel.

        Args:
            name: Name des Kontakts (wird nicht geloggt).
            message: Nachricht des Kontakts.

        Returns:
            True wenn Entscheidungstraeger-Titel erkannt wurde.
        """
        combined = f"{name} {message}".lower()

        # Zuerst auf Nicht-Entscheider pruefen (hoeherer Vorrang)
        if any(title in combined for title in _NON_DECISION_MAKER_TITLES):
            return False

        # Dann auf Entscheider pruefen
        if any(title in combined for title in _DECISION_MAKER_TITLES):
            return True

        return False

    def _classify_industry(
        self,
        text_content: str,
        company_name: str,
        message: str,
    ) -> str:
        """
        Klassifiziert die Branche anhand von Textinhalten.

        Args:
            text_content: Extrahierter Website-Text.
            company_name: Firmenname.
            message: Lead-Nachricht.

        Returns:
            Branchenbezeichnung (z.B. "IT", "Marketing") oder "unknown".
        """
        combined = f"{text_content} {company_name} {message}".lower()
        scores: dict[str, int] = {}

        for industry, keywords in _INDUSTRY_KEYWORDS.items():
            count = sum(1 for kw in keywords if kw in combined)
            if count > 0:
                scores[industry] = count

        if not scores:
            return "unknown"

        return max(scores, key=lambda k: scores[k])

    def _estimate_company_size(self, text_content: str, message: str) -> str:
        """
        Schaetzt die Unternehmensgroesse anhand von Texthinweisen.

        Args:
            text_content: Extrahierter Website-Text.
            message: Lead-Nachricht.

        Returns:
            Groessenbereich als String (z.B. "10-50", "50-200") oder "unknown".
        """
        combined = f"{text_content} {message}".lower()

        # Explizite Mitarbeiteranzahl suchen (z.B. "150 Mitarbeiter")
        match = re.search(r"(\d+)\s*(?:mitarbeiter|employees|ma\b|beschaeftigte)", combined)
        if match:
            count = int(match.group(1))
            if count < 10:
                return "1-9"
            elif count < 50:
                return "10-50"
            elif count < 200:
                return "50-200"
            elif count <= 500:
                return "200-500"
            else:
                return "500+"

        # Keyword-basierte Schaetzung
        if any(kw in combined for kw in ["startup", "gruender", "gründer", "gegründet", "launch"]):
            return "1-50"
        if any(kw in combined for kw in ["mittelstand", "regional", "familienunternehmen"]):
            return "50-200"
        if any(kw in combined for kw in ["konzern", "international", "weltweit", "global", "ag "]):
            return "200-500"

        return "unknown"

    def _generate_linkedin_url(self, company_name: str) -> Optional[str]:
        """
        Generiert eine LinkedIn-Unternehmensseiten-URL aus dem Firmennamen.

        Args:
            company_name: Firmenname.

        Returns:
            LinkedIn-URL oder None wenn kein gueltiger Slug generiert werden kann.
        """
        if not company_name:
            return None

        slug = self._company_to_slug(company_name)
        if not slug:
            return None

        return f"https://www.linkedin.com/company/{slug}"

    async def _check_hiring_status(self, website_url: str, company_name: str) -> dict:
        """
        Ermittelt ob ein Unternehmen aktuell Mitarbeiter sucht.

        Prueft zuerst die Karriereseite auf der eigenen Website (/jobs, /karriere etc.).
        Falls dort kein Ergebnis: Indeed-Suche als Ergaenzung.
        Beide Quellen werden zusammengefuehrt.

        Args:
            website_url: Basis-URL der Unternehmenswebsite (z.B. "https://example.de").
            company_name: Firmenname fuer die Indeed-Suche.

        Returns:
            Dictionary mit is_hiring (bool|None), job_page_url (str|None)
            und open_positions_count (int).
        """
        result: dict = {
            "is_hiring": None,
            "job_page_url": None,
            "open_positions_count": 0,
        }

        # Schritt 1: Eigene Website auf Karriereseite pruefen
        if website_url:
            website_hiring = await self._web_scraper.check_hiring_status(website_url)
            if website_hiring.get("is_hiring") is not None:
                result["is_hiring"] = website_hiring["is_hiring"]
                result["job_page_url"] = website_hiring.get("job_page_url")
                result["open_positions_count"] = website_hiring.get("open_positions_count", 0)

                logger.info(
                    "ResearchAgent: Website-Hiring-Check fuer lead_id=%s: is_hiring=%s, stellen=%d",
                    self._blackboard.lead_id,
                    result["is_hiring"],
                    result["open_positions_count"],
                )

                # Wenn Website eindeutig is_hiring=True: Indeed als Ergaenzung fuer Anzahl
                if result["is_hiring"] and result["open_positions_count"] <= 1:
                    indeed = await self._indeed_checker.search_company_jobs(company_name)
                    if indeed.get("open_positions_count", 0) > result["open_positions_count"]:
                        result["open_positions_count"] = indeed["open_positions_count"]
                return result

        # Schritt 2: Kein Website-Ergebnis -> Indeed als alleinige Quelle
        indeed = await self._indeed_checker.search_company_jobs(company_name)
        if indeed.get("is_hiring") is not None:
            result["is_hiring"] = indeed["is_hiring"]
            result["open_positions_count"] = indeed.get("open_positions_count", 0)
            logger.info(
                "ResearchAgent: Indeed-Hiring-Check fuer lead_id=%s: is_hiring=%s, stellen=%d",
                self._blackboard.lead_id,
                result["is_hiring"],
                result["open_positions_count"],
            )

        return result

    def _extract_location(self, text_content: str) -> Optional[str]:
        """
        Extrahiert den Unternehmensstandort aus dem Website-Text.

        Erkennt DACH-Staedte und Laender anhand von Keywords.

        Args:
            text_content: Extrahierter Website-Text.

        Returns:
            Standortangabe (z.B. "Berlin, Deutschland") oder None.
        """
        if not text_content:
            return None

        text_lower = text_content.lower()

        # Deutsche Grossstaedte
        cities = [
            "berlin", "hamburg", "muenchen", "münchen", "koeln", "köln",
            "frankfurt", "stuttgart", "duesseldorf", "düsseldorf",
            "dortmund", "essen", "bremen", "leipzig", "dresden",
            "hannover", "nuernberg", "nürnberg", "duisburg",
            # Oesterreich
            "wien", "graz", "linz", "salzburg", "innsbruck",
            # Schweiz
            "zuerich", "zürich", "genf", "basel", "bern", "lausanne",
        ]

        for city in cities:
            if city in text_lower:
                # Land ableiten
                at_cities = {"wien", "graz", "linz", "salzburg", "innsbruck"}
                ch_cities = {"zuerich", "zürich", "genf", "basel", "bern", "lausanne"}
                city_clean = city.replace("ue", "ü").replace("oe", "ö").replace("ae", "ä")
                if city in at_cities:
                    return f"{city_clean.capitalize()}, Oesterreich"
                elif city in ch_cities:
                    return f"{city_clean.capitalize()}, Schweiz"
                else:
                    return f"{city_clean.capitalize()}, Deutschland"

        # Land-Erkennung falls keine Stadt gefunden
        if "österreich" in text_lower or "austria" in text_lower:
            return "Oesterreich"
        if "schweiz" in text_lower or "switzerland" in text_lower:
            return "Schweiz"
        if "deutschland" in text_lower or "germany" in text_lower:
            return "Deutschland"

        return None
