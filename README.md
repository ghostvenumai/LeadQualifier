<div align="center">

<img src="https://img.shields.io/badge/LeadQualifier_AI-GhostVenumAI-22c55e?style=for-the-badge&logo=lightning&logoColor=white" />

# LeadQualifier AI

**Vollautomatisches B2B Lead-Qualifizierungssystem für den DACH-Markt**

Eingehender Lead → KI-Analyse → Score → personalisierte E-Mail  
alles in unter 30 Sekunden, vollautomatisch.

[![Python](https://img.shields.io/badge/Python-3.11+-3776ab?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Flask](https://img.shields.io/badge/Flask-3.0+-000000?style=flat-square&logo=flask&logoColor=white)](https://flask.palletsprojects.com)
[![Claude](https://img.shields.io/badge/Claude_AI-Opus_+_Sonnet-a855f7?style=flat-square&logo=anthropic&logoColor=white)](https://anthropic.com)
[![License](https://img.shields.io/badge/Lizenz-Demo_·_Vollversion_auf_Anfrage-22c55e?style=flat-square)](https://github.com/GhostVenumAI)
[![DSGVO](https://img.shields.io/badge/DSGVO-konform-3b82f6?style=flat-square)](https://github.com/GhostVenumAI)

</div>

---

## Was ist LeadQualifier AI?

LeadQualifier AI empfängt eingehende B2B-Kontaktanfragen über einen Webhook, bewertet sie vollautomatisch mit **5 spezialisierten KI-Agents** und sendet eine maßgeschneiderte Antwort-E-Mail — ohne manuellen Aufwand.

> Entwickelt von **Serkan · GhostVenumAI** für den deutschen, österreichischen und Schweizer B2B-Markt.

---

## Features

- **5 KI-Agents** arbeiten parallel: Research, Scoring, Writer, Reviewer, Orchestrator
- **12-Kriterien-Scoring** — jeder Lead wird präzise von 1–10 bewertet
- **Strukturierter JSON-Output** — erzwungenes Tool-Use-Schema, keine Parse-Fehler
- **Selbstlernender Feedback-Loop** — reale Vertriebsergebnisse kalibrieren das Scoring
- **Duplikat-Erkennung** — erkennt automatisch ob eine Firma oder E-Mail bereits eingereicht wurde
- **Hiring-Erkennung** — prüft ob die Firma gerade Mitarbeiter sucht (Website + Indeed)
- **Personalisierte E-Mails** via Claude Opus 4.6 (beste Modellqualität)
- **Asynchroner Webhook** — 202 Accepted + Status-Polling, kein Timeout bei Zapier & Co.
- **DSGVO-konform** — keine E-Mail-Adressen in Logs oder Datenbank
- **Gmail SMTP** — kein OAuth, einfache Einrichtung per App-Passwort
- **Slack-Alerts** mit AES-256-GCM Verschlüsselung
- **Admin-Authentifizierung** — Konfigurations-API per Token geschützt
- **Web-Dashboard** mit Dunkel/Hell-Modus und Mehrsprachigkeit (DE/EN/ES)
- **Produktionsbereit** — Docker, Gunicorn, CI (pytest + ruff), 99 Tests
- **Demo-Modus** — sofort einsatzbereit, ohne Lizenzschlüssel

---

## Duplikat-Erkennung

Das System erkennt automatisch, wenn eine Firma oder eine E-Mail-Adresse bereits eingereicht wurde — bevor die teure KI-Pipeline gestartet wird. Das spart API-Kosten und verhindert doppelte E-Mails an denselben Kontakt.

### Zwei Erkennungsstufen

| Typ | Zeitfenster | Logik |
|---|---|---|
| **E-Mail-Duplikat** | 30 Tage | Exakt selbe E-Mail-Adresse (SHA-256-Hash-Vergleich, DSGVO-konform) |
| **Firmen-Duplikat** | 7 Tage | Selber Firmenname — Rechtsformen werden ignoriert |

### Intelligente Firmennamen-Normalisierung

`Mustermann GmbH`, `Mustermann AG` und `Mustermann GmbH & Co. KG` werden alle als `mustermann` erkannt.  
Ignorierte Rechtsformen: GmbH, AG, UG, KG, OHG, GbR, Ltd, Inc, LLC, Corp, SE, eV und weitere.

### Response bei Duplikat

HTTP `409 Conflict`:

```json
{
  "success": false,
  "error": "duplicate",
  "reason": "Von diesem Unternehmen wurde bereits in den letzten 7 Tagen ein Lead eingereicht.",
  "last_submitted": "2026-05-02T10:30:00+00:00",
  "last_score": 8,
  "last_status": "qualified"
}
```

Die Antwort enthält wann der letzte Lead war, welchen Score er hatte und ob er qualifiziert wurde — so kann das aufrufende System direkt entscheiden was zu tun ist.

---

## Architektur

```
POST /webhook/lead
        │
        ▼
┌─────────────────────┐
│  Validierung &      │  ← Pydantic, Lizenz-Check
│  Duplikat-Check     │  ← E-Mail (30 Tage) + Firma (7 Tage) → HTTP 409
└──────────┬──────────┘
           │  (nur neue, einmalige Leads)
           ▼
┌─────────────────────┐
│  ORCHESTRATOR AGENT │  ← Pipeline-Koordination
└──────────┬──────────┘
           │  asyncio.gather() — parallel
    ┌──────┴──────┐
    ▼             ▼
┌────────┐  ┌─────────┐
│RESEARCH│  │ SCORING │
│ AGENT  │  │  AGENT  │
│        │  │         │
│Website │  │12 Krit. │
│Scraping│  │Score 1-10│
│Indeed  │  │         │
└───┬────┘  └────┬────┘
    └──────┬─────┘
           ▼
    ┌─────────────┐
    │ WRITER AGENT│  ← Claude Opus 4.6
    │             │
    │qualify/reject│
    │E-Mail Draft │
    └──────┬──────┘
           ▼
    ┌──────────────┐
    │REVIEWER AGENT│  ← DSGVO-Check, Ton, Qualität
    └──────┬───────┘
           ▼
    ┌──────────────┐
    │    OUTPUT    │
    │ Gmail · Slack│
    │  CRM · SQLite│
    └──────────────┘
```

---

## Scoring-System (12 Kriterien)

| Kriterium | Punkte | Beschreibung |
|---|---|---|
| `decision_maker` | +3 | CEO, CTO, Geschäftsführer, Inhaber |
| `company_size` | +2 | 10–500 Mitarbeiter (DACH-Zielgröße) |
| `industry_fit` | +2 | IT, Marketing, Beratung, SaaS |
| `budget_signals` | +1 | Budget genannt **oder** Firma stellt gerade ein |
| `urgency` | +1 | Konkreter Zeitrahmen (Q2, Q3, dieses Jahr) |
| `message_quality` | +1 | Spezifische, konkrete Anfrage |
| `website_quality` | +1 | Professionelle Unternehmenswebsite |
| `linkedin_presence` | +1 | LinkedIn-Profil vorhanden |
| `news_activity` | +1 | Aktuelle Nachrichten zu Wachstum/Investment |
| `tech_stack_fit` | +1 | Passendes Tech-Stack erkannt |
| `geo_relevance` | +1 | DACH-Region (D/A/CH) |
| `contact_completeness` | +1 | Vollständige Kontaktdaten |

**Score ≥ 7** → Qualifizierungs-E-Mail  
**Score < 7** → Freundliche Absage

---

## Tech Stack

| Komponente | Technologie |
|---|---|
| Backend | Python 3.11+ · Flask 3.0 · Gunicorn |
| KI | Anthropic Claude API — Opus 4.6 (Writer) · Sonnet 4.6 (Research/Scoring) · Haiku 4.5 (Reviewer) |
| KI-Robustheit | AsyncAnthropic · automatische Retries · Tool-Use-JSON-Schema |
| Datenbank | SQLite (WAL) — lokal, DSGVO-konform |
| E-Mail | Gmail SMTP mit App-Passwort |
| Verschlüsselung | AES-256-GCM (Slack-Payloads) |
| Web-Scraping | httpx · BeautifulSoup4 |
| Validierung | Pydantic v2 |
| Deployment | Docker · docker-compose · Healthchecks |
| Qualität | pytest (99 Tests) · ruff · GitHub Actions CI |

---

## Schnellstart

### Voraussetzungen

- Python 3.11+
- Anthropic API Key ([console.anthropic.com](https://console.anthropic.com))
- Gmail-Konto mit App-Passwort (optional, für E-Mail-Versand)

### Installation

```bash
# Repository klonen
git clone https://github.com/GhostVenumAI/LeadQualifier.git
cd LeadQualifier

# Abhängigkeiten installieren
pip install -r requirements.txt

# Konfiguration anlegen
cp .env.example .env
# ANTHROPIC_API_KEY in .env eintragen
```

### Starten

```bash
python main.py
```

Dashboard öffnen: **http://localhost:5000**

### Ersten Lead testen

```bash
curl -X POST http://localhost:5000/webhook/lead \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Thomas Berger",
    "email": "t.berger@digitalvision.de",
    "company": "DigitalVision GmbH",
    "message": "Ich bin Geschäftsführer und suche eine Lösung zur automatischen Lead-Qualifizierung. Budget ist für Q2 2026 geplant."
  }'
```

---

## Konfiguration

Alle Einstellungen können über das **Web-Dashboard** unter `/settings` gesetzt werden — oder direkt in der `.env`-Datei:

| Variable | Beschreibung | Pflicht |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude API Key | ✅ |
| `GMAIL_SENDER_EMAIL` | Absender-Adresse | Optional |
| `GMAIL_APP_PASSWORD` | Google App-Passwort (16 Zeichen) | Optional |
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook | Optional |
| `SLACK_ENCRYPTION_KEY` | AES-256-GCM Key (64-Hex) | Optional |
| `CRM_WEBHOOK_URL` | HubSpot / Pipedrive Webhook | Optional |
| `WEBHOOK_SECRET` | HMAC-SHA256 Signatur-Secret | Produktion ✅ |
| `ADMIN_TOKEN` | Token für Konfigurations-API (`X-Admin-Token`-Header) | Produktion ✅ |
| `WEBHOOK_ASYNC_MODE` | `true` = Webhook antwortet sofort mit 202 | Optional |
| `TRUSTED_PROXY_COUNT` | Anzahl Reverse-Proxies vor der App (X-Forwarded-For) | Optional |
| `QUALIFY_THRESHOLD` | Score-Schwellenwert (1–10, default: 7) | Optional |
| `CLAUDE_REVIEWER_MODEL` | Modell für den Reviewer (default: Haiku 4.5) | Optional |

Alle weiteren Variablen (Timeouts, Retries, Worker-Anzahl) sind in `.env.example` dokumentiert.

---

## Produktivbetrieb

### Docker (empfohlen)

```bash
cp .env.example .env   # Secrets eintragen
docker compose up -d
```

Die SQLite-Datenbank liegt im Volume `leadqualifier-data` und überlebt Container-Neustarts. Der Container läuft als Non-Root-User mit Healthcheck.

### Gunicorn (ohne Docker)

```bash
pip install -r requirements.txt
gunicorn -c gunicorn.conf.py main:app
```

### Asynchroner Webhook

Webhook-Quellen wie Zapier oder HubSpot brechen nach wenigen Sekunden ab — die Pipeline braucht aber 30–60 s. Mit `WEBHOOK_ASYNC_MODE=true` (oder pro Request `?async=1`) antwortet der Webhook sofort:

```json
HTTP 202 Accepted
{
  "success": true,
  "accepted": true,
  "lead_id": "7f3b9c4e-...",
  "status_url": "/api/leads/7f3b9c4e-.../status"
}
```

Das Ergebnis liefert anschließend `GET /api/leads/<lead_id>/status` — erst `"processing"`, dann `"completed"` inklusive Score, E-Mail-Typ und Kosten.

### Sicherheits-Checkliste für den Produktivbetrieb

1. `WEBHOOK_SECRET` setzen (HMAC-Signaturpflicht für eingehende Leads)
2. `ADMIN_TOKEN` setzen (sonst sind die Settings-Endpunkte nur von localhost erreichbar)
3. `FLASK_SECRET_KEY` setzen
4. Hinter Reverse-Proxy: `TRUSTED_PROXY_COUNT=1` für korrektes Rate-Limiting
5. TLS über den Reverse-Proxy terminieren (nginx, Traefik, Caddy)

---

## Dashboard

Das eingebaute Web-Dashboard bietet:

- **Live-Monitor** — eingehende Leads in Echtzeit
- **Lead-Verwaltung** — alle verarbeiteten Leads mit Score und E-Mail-Typ
- **Lead-Tester** — Testanfragen direkt aus dem Browser senden
- **Einstellungen** — alle API-Keys und Konfiguration über UI setzbar
- **Dunkel/Hell-Modus** — anpassbares Erscheinungsbild
- **Mehrsprachig** — Deutsch, English, Español

---

## Lizenz

LeadQualifier AI läuft ohne Lizenzschlüssel im **Demo-Modus** (10 Leads oder 14 Tage — die Limits sind via `DEMO_MAX_LEADS` / `DEMO_MAX_DAYS` konfigurierbar). Einfach klonen, API-Key eintragen, starten.

Für den unbegrenzten Produktiveinsatz: **Vollversion auf Anfrage** — siehe [LICENSE](LICENSE) und Preismodelle im Dashboard unter `/settings`.

---

## DSGVO & Sicherheit

- E-Mail-Adressen werden **nicht** in der Datenbank gespeichert
- E-Mail-Adressen werden **nicht** geloggt
- Slack-Alerts sind **AES-256-GCM verschlüsselt**
- Webhook-Validierung via **HMAC-SHA256**
- Alle Secrets kommen aus `.env` — **nie hardcoded**
- SQL ausschließlich über **Parameterized Queries**

---

<div align="center">

Made with ❤️ by **Serkan · GhostVenumAI**

[![GitHub](https://img.shields.io/badge/GitHub-GhostVenumAI-181717?style=flat-square&logo=github)](https://github.com/GhostVenumAI)

</div>
