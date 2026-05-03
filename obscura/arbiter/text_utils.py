"""obscura.arbiter.text_utils — Shared keyword-extraction primitives.

Lives at the bottom of the arbiter package so ``checks`` and ``criteria``
can both depend on these without forming a peer cycle. Previously
``criteria`` imported ``_STOP_WORDS`` / ``_stem`` from ``checks`` while
``checks`` ran ``verify_criteria`` from ``criteria`` — the cycle was
papered over with a lazy import inside the function body.
"""

from __future__ import annotations

_STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "and",
        "but",
        "or",
        "not",
        "no",
        "if",
        "then",
        "that",
        "this",
        "it",
        "its",
        "i",
        "me",
        "my",
        "we",
        "our",
        "you",
        "your",
        "he",
        "she",
        "they",
        "them",
        "their",
        "what",
        "which",
        "who",
    }
)


_VOWELS = frozenset("aeiou")


def _stem(word: str) -> str:
    """Lightweight English suffix stripper for keyword matching.

    Not a full Porter stemmer — handles the most common suffixes that
    cause arbiter drift/relevance false negatives. Each rule carries its
    own minimum stem length to avoid over-stripping short words.

    Special cases:
    - After "-ing", doubled trailing consonants are collapsed (running→run).
    - After "-er", stems ending consonant+"l" are rejected (handler stays
      "handler"; checker→check is fine because "ck" is a valid ending).
    - After "-er", doubled trailing consonants are collapsed (runner→run).
    - "-s"/"-es" rules skip if the resulting stem itself ends in "s".
    """
    if len(word) <= 3:
        return word

    # Each entry: (suffix, replacement, min_stem_length_after_strip)
    # Longest suffixes first so they fire before shorter overlapping rules.
    _RULES: tuple[tuple[str, str, int], ...] = (
        # 7-char
        ("ational", "ate", 4),
        # 6-char
        ("tional", "tion", 4),
        ("encies", "ence", 4),
        ("ancing", "ance", 4),
        # 5-char — pluralise before singular so "migrations"→"migrate"
        ("ations", "ate", 4),
        ("ation", "ate", 4),
        ("izing", "ize", 4),
        ("ising", "ise", 4),
        ("ating", "ate", 4),
        ("ities", "ity", 4),
        ("iness", "y", 4),
        ("ments", "", 5),
        ("ators", "ate", 4),
        # 4-char
        ("ness", "", 5),
        ("ment", "", 5),
        ("tion", "", 5),  # min 5: "function"→"func"(4) rejected → stays "function"
        ("sion", "", 5),
        ("ious", "", 5),
        ("eous", "", 5),
        ("ible", "", 5),
        ("able", "", 5),
        ("ally", "", 5),
        ("ical", "", 5),
        ("ator", "ate", 4),
        ("ures", "", 4),  # failures→fail
        # 3-char
        ("ing", "", 4),
        ("ies", "y", 4),
        ("ors", "or", 4),
        ("ers", "er", 4),
        ("ent", "", 5),
        ("ant", "", 5),
        ("ous", "", 5),
        ("ive", "", 5),
        ("ize", "", 4),
        ("ise", "", 4),
        # 2-char
        ("ed", "", 4),
        ("ly", "", 4),
        ("es", "", 5),
        ("er", "", 4),
        # 1-char (last resort)
        ("s", "", 4),
    )

    for suffix, replacement, min_len in _RULES:
        if word.endswith(suffix):
            stem = word[: -len(suffix)] + replacement
            if len(stem) < min_len:
                continue
            # After stripping "-ing": collapse doubled trailing consonant.
            # stopping→stopp→stop, running→runn→run.
            if suffix == "ing" and len(stem) >= 2:
                if stem[-1] == stem[-2] and stem[-1] not in _VOWELS:
                    stem = stem[:-1]
            # "-s"/"-es": skip if stem itself ends in "s" (databases→databas → skip).
            if suffix in ("s", "es") and stem.endswith("s"):
                continue
            # "-er": reject consonant+"l" endings (handler→handl is not a base form).
            # Also de-double trailing consonant (runner→runn→run).
            if suffix == "er" and len(stem) >= 2:
                if stem[-1] == "l" and stem[-2] not in _VOWELS:
                    continue
                if stem[-1] == stem[-2] and stem[-1] not in _VOWELS:
                    stem = stem[:-1]
            return stem
    return word
