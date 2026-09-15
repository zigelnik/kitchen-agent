"""Hebrew kitchen-carpentry glossary used to bias Whisper decoding.

Whisper's `initial_prompt` nudges the decoder toward this vocabulary, which
matters for brand names and trade jargon it would otherwise mangle. Grow this
list from real `transcripts_log` rows (PLAN.md Phase 1).
"""

TERMS: list[str] = [
    # layout / structure
    "מטבח", "ארון", "ארונות", "ארון בסיס", "ארון עליון", "קרן זווית",
    "מטבח בצורת L", "מטבח בצורת U", "מטבח ישר", "אי", "חזיתות",
    # materials
    "מלמין", "פורניר", "אלון", "לכה", "לכה מט", "לכה מבריקה", "MDF",
    "סנדוויץ'", "פורמייקה", "דיקט",
    # countertops
    "משטח", "שיש", "אבן קיסר", "גרניט", "קוורץ", "למינציה",
    # hardware
    "מגירה", "מגירות", "בלום", "האפלה", "ציר", "צירים", "סגירה שקטה",
    "ידית", "ידיות", "כרום", "נירוסטה", "סגסוגת", "ריל", "מסילה",
    "פתיחה ללחיצה", "בוקסה", "פנטוגרף", "מיכל אשפה נשלף",
    # appliances / fittings
    "כיור", "ברז", "תנור", "כיריים", "קולט אדים", "מדיח", "מיקרוגל",
    # commercial
    "הצעת מחיר", "מקדמה", "התקנה", "הובלה", "מטר רץ", "שעות עבודה",
]


def whisper_prompt() -> str:
    """A comma-joined term list — the format Whisper biases on best."""
    return "מטבחים ונגרות: " + ", ".join(TERMS) + "."
