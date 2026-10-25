"""Optional Arabic translation for transcript lines.

Uses Google's public translate endpoint (text only, no API key).  This is
strictly opt-in in the UI: the first time the user enables it they are told
that the English text leaves the machine.  Audio never leaves the machine.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

ENDPOINT = "https://translate.googleapis.com/translate_a/single"


class TranslationError(RuntimeError):
    pass


def translate_ar(text: str, timeout: float = 8.0) -> str:
    """Translate one English sentence to Arabic.  Raises TranslationError."""
    source = " ".join(text.split())
    if not source:
        return ""

    query = urllib.parse.urlencode(
        {
            "client": "gtx",
            "sl": "en",
            "tl": "ar",
            "dt": "t",
            "q": source[:1000],
        }
    )
    request = urllib.request.Request(
        f"{ENDPOINT}?{query}",
        headers={"User-Agent": "Mozilla/5.0 (MeetingCopilot/0.2)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - network errors are opaque
        raise TranslationError(str(exc)) from exc

    try:
        parts = payload[0]
        translated = "".join(part[0] for part in parts if part and part[0])
    except (TypeError, IndexError, KeyError) as exc:
        raise TranslationError("Unexpected translation response") from exc

    result = translated.strip()
    if not result:
        raise TranslationError("Empty translation")
    return result
