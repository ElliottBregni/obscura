"""obscura.kairos.frustration — User frustration detection.

Detects frustration signals in user input for UX adaptation.
When frustration is detected, the agent can adjust its behavior
(e.g., be more careful, ask clarifying questions, apologize).

Pattern from claude-code's ``userPromptKeywords.ts``.
"""

from __future__ import annotations

import re

# Negative/frustration keywords (case-insensitive).
_NEGATIVE_PATTERN = re.compile(
    r"\b("
    r"wtf|wth|ffs|omfg"
    r"|shit(?:ty|tiest)?"
    r"|dumbass"
    r"|horrible|awful"
    r"|piss(?:ed|ing)?\s*off"
    r"|piece\s+of\s+(?:shit|crap|junk)"
    r"|what\s+the\s+(?:fuck|hell)"
    r"|fuck(?:ing)?\s+(?:broken|useless|terrible|awful|horrible)"
    r"|fuck\s+you"
    r"|screw\s+(?:this|you)"
    r"|so\s+frustrating"
    r"|this\s+sucks"
    r"|damn\s+it"
    r"|you(?:'re|\s+are)\s+(?:useless|terrible|broken|stupid)"
    r"|stop\s+(?:doing\s+that|it|this)"
    r"|i\s+(?:hate|can(?:'t|\s*not)\s+stand)\s+(?:this|you)"
    r")\b",
    re.IGNORECASE,
)

# "Keep going" keywords — user wants continuation, not frustration.
_KEEP_GOING_PATTERN = re.compile(
    r"\b(?:keep\s+going|go\s+on|continue)\b",
    re.IGNORECASE,
)

# Positive reinforcement keywords.
_POSITIVE_PATTERN = re.compile(
    r"\b(?:thank(?:s|\s+you)|perfect|great|awesome|excellent|nice|good\s+job|well\s+done|exactly)\b",
    re.IGNORECASE,
)


class FrustrationDetector:
    """Detect frustration and sentiment signals in user input.

    Usage::

        detector = FrustrationDetector()
        result = detector.analyze("wtf this is broken again")
        if result.is_frustrated:
            print("User is frustrated:", result.matched_phrase)
    """

    def __init__(self) -> None:
        self._frustration_count = 0
        self._positive_count = 0
        self._total_messages = 0

    def analyze(self, text: str) -> SentimentResult:
        """Analyze user input for frustration signals."""
        self._total_messages += 1

        # Check for frustration.
        neg_match = _NEGATIVE_PATTERN.search(text)
        if neg_match:
            self._frustration_count += 1
            return SentimentResult(
                sentiment="frustrated",
                is_frustrated=True,
                matched_phrase=neg_match.group(0),
                consecutive_frustrations=self._frustration_count,
            )

        # Check for positive reinforcement.
        pos_match = _POSITIVE_PATTERN.search(text)
        if pos_match:
            self._frustration_count = 0  # Reset streak.
            self._positive_count += 1
            return SentimentResult(
                sentiment="positive",
                is_frustrated=False,
                matched_phrase=pos_match.group(0),
            )

        # Check for "keep going".
        if _KEEP_GOING_PATTERN.search(text):
            return SentimentResult(
                sentiment="continue",
                is_frustrated=False,
                matched_phrase="keep going",
            )

        # Neutral.
        self._frustration_count = 0
        return SentimentResult(sentiment="neutral", is_frustrated=False)

    @property
    def frustration_rate(self) -> float:
        """Fraction of messages that triggered frustration detection."""
        if self._total_messages == 0:
            return 0.0
        return self._frustration_count / self._total_messages

    def reset(self) -> None:
        """Reset all counters."""
        self._frustration_count = 0
        self._positive_count = 0
        self._total_messages = 0


class SentimentResult:
    """Result of frustration/sentiment analysis."""

    __slots__ = (
        "consecutive_frustrations",
        "is_frustrated",
        "matched_phrase",
        "sentiment",
    )

    def __init__(
        self,
        sentiment: str = "neutral",
        is_frustrated: bool = False,
        matched_phrase: str = "",
        consecutive_frustrations: int = 0,
    ) -> None:
        self.sentiment = sentiment
        self.is_frustrated = is_frustrated
        self.matched_phrase = matched_phrase
        self.consecutive_frustrations = consecutive_frustrations
