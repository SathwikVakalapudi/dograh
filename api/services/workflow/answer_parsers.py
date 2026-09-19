"""Deterministic answer parsers for the pre-LLM fast path.

A parser turns a user utterance into a canonical answer, or returns ``None`` to
say "I am not sure" — in which case the caller must fall back to the LLM. They
exist purely to skip inference on unambiguous replies; they are never the
authority on meaning.

Every parser is deliberately conservative: it commits only when exactly one
category matches, so a mixed or hedged utterance ("भाजपा नहीं, कांग्रेस") falls
through to the LLM rather than guessing.

.. warning::
   Parsers are **node-scoped** and must only be applied to nodes that ask the
   matching question. ``parse_party`` on a caste question would happily accept
   "कांग्रेस" as a caste. See ``DETERMINISTIC_ANSWER_NODES`` in
   ``deterministic_answer_gate``.
"""

import re
import unicodedata
from typing import Callable, Optional

# Devanagari + ASCII word characters, including ZWNJ/ZWJ which appear inside
# conjuncts and must not be treated as word boundaries.
#
# The danda (U+0964) and double danda (U+0965) sit inside the Devanagari block
# but are sentence punctuation, and Sarvam ends most utterances with one
# ("कांग्रेस।"). Treating them as word characters would break the trailing word
# boundary and stop every real transcript from matching, so they are carved out.
_WORD_CHAR = r"[0-9A-Za-z_ऀ-ॣ०-ॿ‌‍]"

_WORD_RE_CACHE: dict[str, re.Pattern] = {}


def _has_word(text: str, words: list[str]) -> bool:
    """Whether any of *words* appears in *text* as a whole word or phrase.

    Substring matching would fire on "नो" inside "बनो", so every target is
    bounded by non-word characters. Text and targets are NFC-normalised because
    the same Devanagari string can arrive pre- or post-composed from STT.
    """
    haystack = re.sub(r"\s+", " ", unicodedata.normalize("NFC", text or ""))
    for word in words:
        pattern = _WORD_RE_CACHE.get(word)
        if pattern is None:
            normalized = re.escape(unicodedata.normalize("NFC", word))
            pattern = re.compile(
                f"(?<!{_WORD_CHAR}){normalized}(?!{_WORD_CHAR})", re.IGNORECASE
            )
            _WORD_RE_CACHE[word] = pattern
        if pattern.search(haystack):
            return True
    return False


# Canonical party codes and the surface forms STT actually produces: Hindi and
# English spellings, abbreviations, symbols and leader names all resolve to the
# same code, mirroring the mapping the node prompts already instruct the LLM to
# apply. Bare "आप" is excluded on purpose — it is ordinary Hindi for "you".
_PARTY_KEYWORDS: list[tuple[str, list[str]]] = [
    (
        "INC",
        [
            "कांग्रेस",
            "काँग्रेस",
            "कांग्रेसी",
            "हाथ",
            "पंजा",
            "राहुल",
            "सुक्खू",
            "सुखविंदर",
            "प्रतिभा",
            "विक्रमादित्य",
            "congress",
            "inc",
        ],
    ),
    (
        "BJP",
        [
            "भाजपा",
            "बीजेपी",
            "बी जे पी",
            "कमल",
            "फूल",
            "मोदी",
            "नरेंद्र मोदी",
            "जयराम",
            "bjp",
            "bharatiya janata party",
        ],
    ),
    (
        "Other",
        [
            "कोई और",
            "किसी और",
            "अन्य",
            "दूसरी",
            "और पार्टी",
            "नोटा",
            "nota",
            "किसी को नहीं",
            "कोई नहीं",
            "कोई पार्टी नहीं",
            "आम आदमी",
            "केजरीवाल",
            "झाड़ू",
            "आप पार्टी",
            "aam aadmi",
            "aap party",
            "बसपा",
            "निर्दलीय",
            "आज़ाद",
        ],
    ),
]


def parse_party(text: str) -> Optional[str]:
    """Return ``INC`` / ``BJP`` / ``Other``, or ``None`` when unsure.

    Returns ``None`` — deferring to the LLM — whenever the utterance mentions
    more than one party, mentions none, or is empty. A correction like
    "भाजपा नहीं, कांग्रेस" therefore reaches the LLM, which is the only thing
    equipped to resolve it.
    """
    if not text or not text.strip():
        return None

    hits = [code for code, keywords in _PARTY_KEYWORDS if _has_word(text, keywords)]
    if len(hits) == 1:
        return hits[0]
    return None


# Parser kinds selectable from configuration. Phase 1 ships party answers only.
PARSERS: dict[str, Callable[[str], Optional[str]]] = {
    "party": parse_party,
}
