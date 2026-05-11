"""
PromptManager: Laed und speichert Agent-System-Prompts.

Prompts werden in prompts.json gespeichert. Fehlt ein Eintrag oder die Datei,
wird der hardcodierte Default verwendet. So sind Anpassungen ohne Code-Aenderung
moeglich und das System bleibt immer funktionsfaehig.
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_PROMPTS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts.json")

DEFAULTS: dict[str, str] = {
    "scoring_system": (
        "Du bist ein erfahrener B2B-Vertriebsanalyst fuer den DACH-Markt "
        "(Deutschland, Oesterreich, Schweiz).\n"
        "Deine Aufgabe ist es, eingehende B2B-Leads praezise und objektiv zu bewerten.\n"
        "Du antwortest AUSSCHLIESSLICH mit einem validen JSON-Objekt - "
        "kein Freitext davor oder danach."
    ),
    "qualify_system": (
        "Du bist ein erfahrener B2B-Vertriebsprofi bei GhostVenumAI im DACH-Markt.\n"
        "Du schreibst professionelle, personalisierte Qualifizierungs-E-Mails auf Deutsch.\n\n"
        "Deine E-Mails sind:\n"
        "- Professionell und auf Augenhoehe mit dem Empfaenger\n"
        "- Konkret und auf die spezifische Situation des Unternehmens eingegangen\n"
        "- Kurz und praegnant (max. 250 Woerter im Body)\n"
        "- Klar mit einem Call-to-Action (z.B. Termin, Demo, Gespraech)\n"
        "- Kein generisches 'Schablonen-Gefuehl'\n\n"
        "Du antwortest AUSSCHLIESSLICH mit einem JSON-Objekt, kein Freitext davor oder danach."
    ),
    "reject_system": (
        "Du bist ein erfahrener B2B-Vertriebsprofi bei GhostVenumAI im DACH-Markt.\n"
        "Du schreibst professionelle, respektvolle Absage-E-Mails auf Deutsch.\n\n"
        "Deine Absage-E-Mails sind:\n"
        "- Respektvoll und wertschaetzend\n"
        "- Kurz (max. 120 Woerter im Body)\n"
        "- Ohne Begruendung (kein 'du bist zu klein' etc.)\n"
        "- Lassen die Tuer fuer die Zukunft offen\n"
        "- Professionell und menschlich\n\n"
        "Du antwortest AUSSCHLIESSLICH mit einem JSON-Objekt, kein Freitext davor oder danach."
    ),
    "reviewer_system": (
        "Du bist ein strenger Qualitaetspruefer fuer professionelle B2B-E-Mails "
        "im deutschsprachigen Raum.\n"
        "Du pruefst E-Mails auf Qualitaet und gibst ein strukturiertes JSON-Feedback.\n"
        "Du antwortest AUSSCHLIESSLICH mit einem validen JSON-Objekt - "
        "kein Freitext davor oder danach."
    ),
}

LABELS: dict[str, str] = {
    "scoring_system":   "Scoring Agent – System-Prompt",
    "qualify_system":   "Writer Agent – Qualifizierungs-E-Mail",
    "reject_system":    "Writer Agent – Absage-E-Mail",
    "reviewer_system":  "Reviewer Agent – System-Prompt",
}


def _load_file() -> dict[str, str]:
    """Laedt prompts.json. Gibt leeres Dict zurueck wenn Datei fehlt oder defekt."""
    try:
        if os.path.exists(_PROMPTS_FILE):
            with open(_PROMPTS_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        logger.warning("PromptManager: prompts.json nicht lesbar: %s", type(exc).__name__)
    return {}


def get(key: str) -> str:
    """
    Gibt den System-Prompt fuer einen Agent-Key zurueck.

    Verwendet den gespeicherten Wert aus prompts.json wenn vorhanden,
    sonst den hardcodierten Default.

    Args:
        key: Prompt-Key (z.B. 'scoring_system').

    Returns:
        System-Prompt als String.
    """
    custom = _load_file().get(key, "").strip()
    if custom:
        return custom
    return DEFAULTS.get(key, "")


def get_all() -> dict[str, dict[str, str]]:
    """
    Gibt alle Prompts mit Label, aktuellem Inhalt und Default zurueck.

    Returns:
        Dict keyed by prompt-key mit label, content, default, is_modified.
    """
    saved = _load_file()
    result = {}
    for key, default in DEFAULTS.items():
        custom = saved.get(key, "").strip()
        result[key] = {
            "label":       LABELS.get(key, key),
            "content":     custom if custom else default,
            "default":     default,
            "is_modified": bool(custom and custom != default),
        }
    return result


def save(key: str, content: str) -> bool:
    """
    Speichert einen angepassten System-Prompt in prompts.json.

    Ist der Inhalt identisch mit dem Default oder leer, wird der Eintrag
    aus der Datei entfernt (Reset auf Default).

    Args:
        key: Prompt-Key.
        content: Neuer Prompt-Inhalt.

    Returns:
        True bei Erfolg.
    """
    try:
        data = _load_file()
        stripped = content.strip()

        if not stripped or stripped == DEFAULTS.get(key, "").strip():
            data.pop(key, None)  # Auf Default zuruecksetzen
        else:
            data[key] = stripped

        with open(_PROMPTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        logger.info("PromptManager: Prompt '%s' gespeichert.", key)
        return True

    except Exception as exc:
        logger.error("PromptManager: Speichern fehlgeschlagen: %s", type(exc).__name__)
        return False


def reset(key: Optional[str] = None) -> bool:
    """
    Setzt einen oder alle Prompts auf den Default zurueck.

    Args:
        key: Prompt-Key oder None fuer alle.

    Returns:
        True bei Erfolg.
    """
    try:
        if key is None:
            if os.path.exists(_PROMPTS_FILE):
                os.remove(_PROMPTS_FILE)
        else:
            data = _load_file()
            data.pop(key, None)
            with open(_PROMPTS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info("PromptManager: Reset fuer key='%s'.", key or "alle")
        return True
    except Exception as exc:
        logger.error("PromptManager: Reset fehlgeschlagen: %s", type(exc).__name__)
        return False
