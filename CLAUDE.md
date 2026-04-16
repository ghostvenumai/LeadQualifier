# LeadQualifier AI - Projektkontext fuer Claude Code

**Projekt:** LeadQualifier AI
**Entwickler:** Serkan (GhostVenumAI)
**Stand:** April 2026
**Beschreibung:** Vollautomatisches B2B Lead-Qualifizierungssystem fuer den DACH-Markt mit 5 Claude-AI-Agents

---

## Was ist LeadQualifier AI?

LeadQualifier AI ist ein Python/Flask-System das eingehende B2B-Anfragen (Leads) vollautomatisch bewertet, qualifiziert und personalisierte Antwort-E-Mails erstellt. Das System nutzt 5 spezialisierte KI-Agents die ueber ein SharedBlackboard kommunizieren.

**Kernfunktion:** Webhook-Eingang -> KI-Analyse -> Scoring -> E-Mail-Versand (alles innerhalb von ~30 Sekunden)

**Zielgruppe:** DACH B2B-Unternehmen (Deutschland, Oesterreich, Schweiz)

---

## Architektur: 5-Agent-System mit SharedBlackboard

```
Eingehender Webhook (POST /webhook/lead)
           |
           v
   [1] ORCHESTRATOR AGENT
   - Validiert eingehende Daten (Pydantic)
   - Prueft Lizenzstatus
   - Erstellt LeadBlackboard
   - Koordiniert alle anderen Agents
           |
           v
   asyncio.gather() - PARALLEL:
   +-----------------+------------------+
   |                 |                  |
   v                 v                  |
[2] RESEARCH      [3] SCORING           |
    AGENT             AGENT             |
  - Website-        - 12 Kriterien      |
    Analyse           bewertet          |
  - Firmen-         - Score 1-10        |
    Recherche       - Begruendung       |
  - LinkedIn-         erstellt         |
    Check                               |
   |                 |                  |
   +-----------------+                  |
           |                            |
           v                            |
   [4] WRITER AGENT                     |
   - Liest Research + Score             |
   - Claude Opus 4.6 (beste Qualitaet)  |
   - "qualify" oder "reject" E-Mail     |
   - Personalisiert auf Lead-Daten      |
           |
           v
   [5] REVIEWER AGENT
   - Prueft E-Mail auf Qualitaet
   - DSGVO-Check (keine PII-Verletzungen)
   - Ton und Professionalitaet
   - Gibt Feedback oder Freigabe
           |
           v
   OUTPUT
   - E-Mail-Versand via Gmail API
   - Slack-Alert (AES-256-GCM verschluesselt)
   - CRM-Webhook-Update
   - SQLite-Persistenz
```

---

## Pipeline-Datenfluss: LeadBlackboard

Das `LeadBlackboard`-Dataclass in `core/blackboard.py` ist das zentrale Shared-Memory-Objekt. Kein Agent kommuniziert direkt mit einem anderen - alle Daten fliessen ueber das Blackboard.

```python
# Felder und welcher Agent sie befuellt:
lead_id, name, email, company, message  # Orchestrator (aus Webhook)
research                                 # Research Agent
score, score_reasoning, score_details   # Scoring Agent
draft_email, email_type                 # Writer Agent
review_passed, review_feedback,
final_email                             # Reviewer Agent
agent_log                               # Alle Agents (Audit-Trail)
```

---

## Scoring-System: 12 Kriterien

Der Scoring Agent bewertet jeden Lead nach 12 Kriterien. Jedes Kriterium wird mit 0 oder 1 bewertet (gesamt max 12, normalisiert auf 1-10).

| # | Kriterium | Beschreibung |
|---|-----------|--------------|
| 1 | decision_maker | Ist der Kontakt Entscheidungstraeger? |
| 2 | company_size | Unternehmensgroesse (Zielgroesse: 10-500 MA) |
| 3 | industry_fit | Passt die Branche zum Angebot? |
| 4 | budget_signals | Hinweise auf verfuegbares Budget? |
| 5 | urgency | Zeitdruck / akute Problemstellung? |
| 6 | message_quality | Qualitaet und Spezifitaet der Nachricht |
| 7 | website_quality | Qualitaet der Unternehmenswebsite |
| 8 | linkedin_presence | Praesenz auf LinkedIn vorhanden? |
| 9 | news_activity | Aktuelle Unternehmensnachrichten? |
| 10 | tech_stack_fit | Passt der bestehende Tech-Stack? |
| 11 | geo_relevance | DACH-Region (bevorzugt) |
| 12 | contact_completeness | Vollstaendigkeit der Kontaktdaten |

**Threshold:** Score >= 7 = Qualifiziert (konfigurierbar via QUALIFY_THRESHOLD in .env)

---

## Tech Stack

| Komponente | Technologie |
|-----------|-------------|
| Sprache | Python 3.11+ |
| Web-Framework | Flask >= 3.0.0 |
| AI-API | Anthropic Claude API |
| Writer Agent Modell | claude-opus-4-6 (beste Qualitaet) |
| Alle anderen Agents | claude-sonnet-4-6 |
| Async | asyncio + asyncio.gather() |
| Datenvalidierung | Pydantic v2 |
| Datenbank | SQLite (via direktes sqlite3) |
| E-Mail-Versand | Gmail API (google-api-python-client) |
| Verschluesselung | AES-256-GCM (cryptography-Library) |
| HTTP-Client | httpx (async) + requests |
| Web-Scraping | beautifulsoup4 |
| Umgebungsvariablen | python-dotenv |
| Tests | pytest + pytest-asyncio |

---

## Datenbankschema (SQLite)

```sql
-- Unternehmen-Cache fuer Research-Ergebnisse
CREATE TABLE companies (
    id TEXT PRIMARY KEY,          -- UUID
    name TEXT NOT NULL,
    website_url TEXT,
    industry TEXT,
    size TEXT,
    website_quality TEXT,
    linkedin_url TEXT,
    tech_stack TEXT,              -- JSON-Array
    news_items TEXT,              -- JSON-Array
    location TEXT,
    annual_revenue TEXT,
    last_updated TIMESTAMP,
    created_at TIMESTAMP
);

-- Leads mit Pipeline-Ergebnissen
CREATE TABLE leads (
    id TEXT PRIMARY KEY,          -- UUID
    name TEXT NOT NULL,
    company TEXT NOT NULL,
    -- KEIN email-Feld in SQLite (DSGVO: minimale Datenspeicherung)
    source TEXT,
    score INTEGER,
    email_type TEXT,
    email_sent BOOLEAN,
    slack_notified BOOLEAN,
    duration_seconds REAL,
    created_at TIMESTAMP,
    processed_at TIMESTAMP
);

-- Feedback fuer Score-Verbesserungen (ML-Loop)
CREATE TABLE scoring_feedback (
    id TEXT PRIMARY KEY,
    lead_id TEXT,
    original_score INTEGER,
    corrected_score INTEGER,
    feedback_reason TEXT,
    created_at TIMESTAMP,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);
```

---

## Lizenzmodell

| Typ | Limits | Beschreibung |
|-----|--------|--------------|
| demo | 10 Leads ODER 14 Tage | Automatisch aktiv wenn kein LICENSE_KEY gesetzt |
| starter | 100 Leads/Monat | Kleinunternehmen |
| professional | 1000 Leads/Monat | Wachsende Unternehmen |
| enterprise | Unlimitiert | Grosskunden |

**Demo-Logik:** Das System deaktiviert sich automatisch sobald EINES der beiden Limits erreicht wird (10 Leads ODER 14 Tage - was zuerst eintritt).

---

## Projektstruktur

```
LeadQualifier/
├── .env                    # NIEMALS committen!
├── .env.example            # Vorlage ohne echte Werte
├── .gitignore
├── CLAUDE.md               # Diese Datei
├── requirements.txt
├── main.py                 # Flask App + Webhook Entry Point
├── core/
│   ├── __init__.py
│   ├── blackboard.py       # LeadBlackboard Dataclass (SharedMemory)
│   ├── config.py           # Settings + Env-Variablen
│   └── models.py           # Pydantic v2 Datenmodelle
├── agents/
│   ├── __init__.py
│   ├── orchestrator.py     # Pipeline-Koordination
│   ├── research.py         # Firmen-Recherche + Caching
│   ├── scoring.py          # 12-Kriterien Bewertung
│   ├── writer.py           # E-Mail-Erstellung (Opus)
│   └── reviewer.py         # Qualitaets-Review
├── integrations/
│   ├── __init__.py
│   ├── gmail.py            # Gmail API Wrapper
│   ├── slack.py            # Slack Alerts (AES-256-GCM)
│   ├── crm.py              # CRM Webhook Client
│   └── license.py          # Lizenz-Validierung
└── tests/
    ├── __init__.py
    ├── test_blackboard.py
    ├── test_models.py
    ├── test_config.py
    ├── test_agents.py
    └── conftest.py
```

---

## Coding Standards (WICHTIG - immer einhalten)

### Pflicht bei jeder Datei:
- **Type Hints** bei allen Funktionen und Methoden (Parameter + Rueckgabewert)
- **Docstrings** bei allen Klassen und Methoden (Google-Style)
- **Error Handling** mit try/except und strukturiertem Logging
- **Kein Logging von sensiblen Daten** (keine API-Keys, keine E-Mail-Adressen, keine Passwörter)

### DSGVO-Compliance:
- E-Mail-Adressen werden NICHT in SQLite gespeichert (Minimalprinzip)
- E-Mail-Adressen werden NICHT geloggt
- Logs enthalten nur Lead-IDs, keine Namen wenn moeglich
- Keine Weitergabe von Personendaten an externe Services ausser notwendig
- Slack-Alerts enthalten keine vollstaendigen E-Mail-Adressen

### Sicherheit:
- Webhook-Validierung via HMAC-SHA256 (WEBHOOK_SECRET)
- Slack-Payloads AES-256-GCM verschluesselt
- Alle Secrets kommen aus .env (nie hardcoded)
- SQL nur via Parameterized Queries (kein String-Formatting)

---

## Agents die in diesem Projekt helfen

| Agent | Rolle |
|-------|-------|
| flask-agent | Flask-Routen, Middleware, Error Handler |
| security-reviewer | DSGVO-Check, HMAC, Verschluesselung |
| test-writer | pytest Tests fuer alle Module |
| code-reviewer | Code-Qualitaet, Type Hints, Docstrings |
| doc-updater | CLAUDE.md und Inline-Docs aktuell halten |
| ghostflow-agent | GhostVenumAI-spezifische Geschaeftslogik |

---

## Wichtige Hinweise

1. `.env` wird NIEMALS committed (steht in .gitignore)
2. Bei jedem neuen Modul: `git commit -m "feat: add [module]"` empfohlen
3. Beim Testen mit Demo-Daten: Echte E-Mail-Adressen vermeiden
4. Der `research`-Dict im Blackboard sollte immer alle Schluesseln aus `ResearchData` enthalten
5. `asyncio.gather()` fuer Research + Scoring ist Pflicht (Parallelitaet = Performance)
6. Claude Opus 4.6 NUR fuer Writer Agent (teurer aber beste Qualitaet)
7. Alle anderen Agents: Claude Sonnet 4.6 (schneller und kostenguenstiger)
