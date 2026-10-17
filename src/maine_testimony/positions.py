"""Extract a submitter's position (support / oppose / neither) from testimony text.

Maine testimony nearly always states the position in its first lines, often in a
standard header ("Testimony in Support of LD 1234", "Neither For Nor Against").
This first pass is a transparent, ordered regex classifier that records the
evidence span it matched. Documents it cannot decide are labeled ``unknown`` and
go to the optional LLM fallback in ``llm_positions`` (``mtestimony positions --llm``).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from enum import StrEnum

from pypdf import PdfReader

HEAD_CHARS = 3000  # positions are stated up front; scanning further adds noise

_BILL = r"(?:L\.?\s?D\.?|LD|bill|H\.?P\.?|S\.?P\.?|this\s+(?:bill|legislation|proposal))"


class Position(StrEnum):
    SUPPORT = "support"
    OPPOSE = "oppose"
    NEITHER = "neither"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PositionResult:
    position: Position
    evidence: str | None = None
    char_offset: int | None = None
    method: str = "regex"


_NEITHER = [
    r"neither\s+for,?\s+n?or\s+against",
    r"\bNFNA\b",
    r"neither\s+in\s+(?:support|favor)\s+(?:of\s+)?n?or\s+(?:in\s+)?opposition",
    r"\b(?:testimony|testify(?:ing)?)\s+(?:is\s+)?(?:as\s+)?neutral\b",
]
# Bill sponsors "introduce" or "present" their bill: implicit support, tracked separately.
_SPONSOR = re.compile(
    r"\b(?:introducing|presenting|sponsoring|co-?sponsoring)\s+(?:of\s+)?" + _BILL, re.I
)
_OPPOSE = [
    r"\bin\s+(?:strong\s+)?opposition\s+(?:to|of)\b",
    r"\bopposition\s+to\s+" + _BILL,
    r"\burge\s+(?:the\s+committee\s+|you\s+)?to\s+(?:reject|vote\s+against|defeat)\b",
    r"\b(?:strongly\s+|respectfully\s+)?oppos(?:e|es|ing)\s+" + _BILL,
    r"\b(?:strongly\s+)?opposed\s+to\b",
    r"\bought\s+not\s+to\s+pass\b",
    r"\bONTP\b",
    r"\bvote\s+(?:no|against)\b",
    r"\bagainst\s+" + _BILL,
    r"\bI\s+(?:strongly\s+)?oppose\b",
    r"\bwe\s+(?:strongly\s+)?oppose\b",
    # "I'm testifying against the passing of LD 104", "here to speak against these bills"
    r"\b(?:testify(?:ing)?|speak(?:ing)?)\s+(?:today\s+)?against\b",
    # "DO NOT PASS" as a header after the LD number or as its own sentence; not
    # "if we do not pass this bill" (which argues for passage)
    r"(?:\bplease\s+|\bL\.?\s?D\.?\s?\d+|[.!?:\u2014\u2013-])[\s:,\"\u201c'-]*do\s+not\s+pass\b",
    r"\bplease\s+(?:vote\s+to\s+)?reject\b",
]
_SUPPORT = [
    r"\bin\s+(?:strong\s+|full\s+)?support\s+of\b",
    r"\b(?:strongly\s+)?support(?:s|ing)?\s+(?:of\s+)?(?:passage|" + _BILL + r")",
    r"\bin\s+favor\s+of\b",
    r"\bsupport\s+for\s+" + _BILL,
    r"\burge\s+(?:the\s+committee\s+|you\s+)?to\s+(?:pass|support|vote\s+(?:yes|for))\b",
    r"\bought\s+to\s+pass\b",
    r"\bOTP(?:-?AM)?\b",
    r"\bvote\s+(?:yes|in\s+favor)\b",
    r"\b(?:I|we)\s+(?:strongly\s+|fully\s+|wholeheartedly\s+)?support\b"
    r"(?!\s+(?:the\s+)?(?:intent|goals?|spirit|concept|idea|aims?|purpose))",
]

_COMPILED = {
    Position.NEITHER: [re.compile(p, re.I) for p in _NEITHER],
    Position.OPPOSE: [re.compile(p, re.I) for p in _OPPOSE],
    Position.SUPPORT: [re.compile(p, re.I) for p in _SUPPORT],
}


def _snippet(text: str, start: int, end: int, pad: int = 60) -> str:
    return text[max(0, start - pad) : end + pad].strip()


def head_text(text: str) -> str:
    """The whitespace-collapsed opening that every classifier (regex or LLM) reads."""
    return re.sub(r"\s+", " ", text)[:HEAD_CHARS]


def classify_position(text: str | None) -> PositionResult:
    """Classify a document's stated position from its text."""
    if not text or not text.strip():
        return PositionResult(Position.UNKNOWN, method="no_text")
    head = head_text(text)

    # "Neither for nor against" contains words that look like both; check it first.
    for pat in _COMPILED[Position.NEITHER]:
        if m := pat.search(head):
            return PositionResult(Position.NEITHER, _snippet(head, *m.span()), m.start())

    # Otherwise the earliest explicit statement wins: headers come before any
    # "we would support it if amended" caveats later in the text.
    best: tuple[int, Position, re.Match[str]] | None = None
    for pos in (Position.OPPOSE, Position.SUPPORT):
        for pat in _COMPILED[pos]:
            m = pat.search(head)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), pos, m)
    sponsor = _SPONSOR.search(head)
    if sponsor and (best is None or sponsor.start() < best[0]):
        return PositionResult(
            Position.SUPPORT, _snippet(head, *sponsor.span()), sponsor.start(), "regex_sponsor"
        )
    if best:
        _, pos, m = best
        return PositionResult(pos, _snippet(head, *m.span()), m.start())
    return PositionResult(Position.UNKNOWN, method="regex_no_match")


def pdf_text(data: bytes) -> tuple[str, int]:
    """Return (text, page_count). Raises on unreadable PDFs."""
    reader = PdfReader(io.BytesIO(data))
    pages = [p.extract_text() or "" for p in reader.pages]
    return "\n".join(pages).replace("\x00", ""), len(pages)
