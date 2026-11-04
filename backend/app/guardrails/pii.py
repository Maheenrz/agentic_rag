"""PII detection and per-entity policy enforcement.

This module detects common structured identifiers with regex/checksums and
adds best-effort person/address/location detection.  If Microsoft Presidio and
its NLP dependencies are installed, its NER recognizers are used as an extra
layer; otherwise conservative contextual heuristics are used.

Important: no detector catches every name or address in every language. Treat
this as defence in depth, not a guarantee that all PII is detected.  Keep the
same public API (KINDS, DEFAULT_ACTIONS, PiiMatch, find_pii, apply_policy) so
existing callers remain compatible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

KINDS = (
    "card", "cnic", "iban", "ssn", "email", "phone", "ip",
    "person", "address", "location",
)

# Sensitive identifiers are masked by default. Contact details remain allowed
# for backwards compatibility; organisations can set any type to mask or block.
DEFAULT_ACTIONS = {
    "card": "mask",
    "cnic": "mask",
    "iban": "mask",
    "ssn": "mask",
    "email": "allow",
    "phone": "allow",
    "ip": "allow",
    "person": "mask",
    "address": "mask",
    "location": "mask",
}


@dataclass(frozen=True)
class PiiMatch:
    kind: str
    start: int
    end: int


def _luhn(digits: str) -> bool:
    total, flip = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if flip:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
        flip = not flip
    return total % 10 == 0


def _iban_ok(raw: str) -> bool:
    s = re.sub(r"\s", "", raw).upper()
    if not 15 <= len(s) <= 34 or not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", s):
        return False
    rearranged = s[4:] + s[:4]
    remainder = 0
    for char in rearranged:
        expanded = str(ord(char) - ord("A") + 10) if char.isalpha() else char
        for digit in expanded:
            remainder = (remainder * 10 + int(digit)) % 97
    return remainder == 1


_CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
_CNIC_DASHED = re.compile(r"(?<!\d)\d{5}-\d{7}-\d(?!\d)")
_CNIC_CONTEXT = re.compile(r"(?i)\b(?:cnic|nic|id\s*card)\b\D{0,15}(\d{13})(?!\d)")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")
_SSN = re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_PHONE_PK = re.compile(r"(?<![\d+A-Za-z-])(?:\+92|0092|0)[-\s]?3\d{2}[-\s]?\d{7}(?!\d)")
_PHONE_INTL = re.compile(r"(?<![\d+])\+\d{1,3}[\s.-]?\(?\d{2,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{3,4}(?!\d)")
_IPV4 = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?![\d.])")

# Contextual name detection is intentionally used instead of treating every
# pair of capitalised words as a name (which causes many false positives).
_NAME = r"[A-Z][A-Za-z'’-]{1,}(?:\s+[A-Z][A-Za-z'’-]{1,}){1,3}"
_NAME_CUE_PATTERNS = (
    re.compile(rf"(?i:\b(?:my name is|name is|full name is|customer name|patient name|employee name|contact person)\s*[:,-]?\s*)({_NAME})"),
    re.compile(rf"\b({_NAME})\s+(?i:lives at|resides at|is reachable at|can be contacted at|'s address is)"),
    re.compile(rf"(?i:\b(?:mr|mrs|ms|miss|dr)\.?\s+)({_NAME})"),
)

_ADDRESS_START = re.compile(
    r"(?i)\b(?:house\s*(?:no\.?\s*)?|h\.?\s*no\.?\s*|flat\s*(?:no\.?\s*)?|"
    r"apartment\s*(?:no\.?\s*)?|apt\.?\s*|plot\s*(?:no\.?\s*)?|"
    r"street\s*(?:no\.?\s*)?|st\.?\s*|road\s*|rd\.?\s*|avenue\s*|"
    r"block\s*|sector\s*|phase\s*|colony\s*|village\s*|postal address\s*[:=-]?\s*)"
)
_ADDRESS_CUE = re.compile(r"(?i)\b(?:home address|mailing address|residential address|address is|lives at|resides at)\s*[:=-]?\s*")
_ADDRESS_PART = re.compile(r"[^\n.;]{1,100}")

# NER is optional so the app still starts in a minimal deployment. Presidio's
# AnalyzerEngine may require a separately installed spaCy language model.
_NER_UNINITIALIZED = object()
_ner_engine: Any = _NER_UNINITIALIZED


def _get_ner_engine() -> Any | None:
    global _ner_engine
    if _ner_engine is not _NER_UNINITIALIZED:
        return _ner_engine
    try:
        from presidio_analyzer import AnalyzerEngine  # type: ignore[import-not-found]

        _ner_engine = AnalyzerEngine()
    except Exception:
        # Missing package/model or incompatible optional setup: use local rules.
        _ner_engine = None
    return _ner_engine


def _find_ner_matches(text: str) -> list[PiiMatch]:
    engine = _get_ner_engine()
    if engine is None or not text.strip():
        return []
    try:
        results = engine.analyze(
            text=text,
            language="en",
            entities=["PERSON", "LOCATION", "GPE", "LOC", "FACILITY", "ADDRESS"],
        )
    except Exception:
        return []

    matches: list[PiiMatch] = []
    for result in results:
        entity = str(getattr(result, "entity_type", "")).upper()
        if entity == "PERSON":
            kind = "person"
        elif entity in {"ADDRESS", "FACILITY"}:
            kind = "address"
        elif entity in {"LOCATION", "GPE", "LOC"}:
            kind = "location"
        else:
            continue
        start, end = int(result.start), int(result.end)
        if 0 <= start < end <= len(text):
            matches.append(PiiMatch(kind, start, end))
    return matches


def _find_contextual_names(text: str) -> list[PiiMatch]:
    found: list[PiiMatch] = []
    for pattern in _NAME_CUE_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span(1)
            if start < end:
                found.append(PiiMatch("person", start, end))
    return found


def _find_addresses(text: str) -> list[PiiMatch]:
    found: list[PiiMatch] = []

    # Address labels capture the value following the label, not the label itself.
    for match in _ADDRESS_CUE.finditer(text):
        value_start = match.end()
        value_match = _ADDRESS_PART.match(text, value_start)
        if value_match:
            raw = value_match.group(0).strip()
            # Avoid treating an empty or very short value as an address.
            if len(raw) >= 6 and re.search(r"\d|,|\b(?:street|road|house|flat|block|sector|lahore|karachi|islamabad)\b", raw, re.I):
                end = value_start + len(raw)
                while end > value_start and text[end - 1].isspace():
                    end -= 1
                found.append(PiiMatch("address", value_start, end))

    # Structured addresses such as "House 12, Street 4, Gulberg, Lahore".
    for match in _ADDRESS_START.finditer(text):
        start = match.start()
        value_match = _ADDRESS_PART.match(text, start)
        if not value_match:
            continue
        raw = value_match.group(0).strip()
        # Require address-like evidence to reduce false positives for ordinary
        # uses of words like "street" or "road" in prose.
        has_number = bool(re.search(r"\d", raw))
        has_multiple_parts = raw.count(",") >= 1
        if len(raw) >= 7 and (has_number or has_multiple_parts):
            end = start + len(raw)
            while end > start and text[end - 1].isspace():
                end -= 1
            found.append(PiiMatch("address", start, end))
    return found


def find_pii(text: str) -> list[PiiMatch]:
    """Return non-overlapping PII spans in *text*.

    Names are detected through optional NER plus contextual rules; addresses
    use optional NER plus address-pattern heuristics. Plain unlabelled names and
    free-form addresses can still be missed, especially in non-English text.
    """
    if not isinstance(text, str) or not text:
        return []

    found: list[PiiMatch] = []
    for match in _IBAN.finditer(text):
        if _iban_ok(match.group(0)):
            found.append(PiiMatch("iban", match.start(), match.end()))

    for match in _CARD.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and len(set(digits)) > 1 and _luhn(digits):
            found.append(PiiMatch("card", match.start(), match.end()))

    for match in _CNIC_DASHED.finditer(text):
        found.append(PiiMatch("cnic", match.start(), match.end()))
    for match in _CNIC_CONTEXT.finditer(text):
        found.append(PiiMatch("cnic", match.start(1), match.end(1)))
    for match in _SSN.finditer(text):
        found.append(PiiMatch("ssn", match.start(), match.end()))
    for match in _EMAIL.finditer(text):
        found.append(PiiMatch("email", match.start(), match.end()))
    for pattern in (_PHONE_PK, _PHONE_INTL):
        for match in pattern.finditer(text):
            found.append(PiiMatch("phone", match.start(), match.end()))
    for match in _IPV4.finditer(text):
        found.append(PiiMatch("ip", match.start(), match.end()))

    found.extend(_find_ner_matches(text))
    found.extend(_find_contextual_names(text))
    found.extend(_find_addresses(text))

    priority = {kind: index for index, kind in enumerate((
        "iban", "card", "cnic", "ssn", "email", "phone", "ip", "address", "person", "location"
    ))}
    # Prefer the longest span when entities start at the same point (e.g. a full
    # address over a shorter location within it), then remove overlaps.
    found.sort(key=lambda item: (item.start, -(item.end - item.start), priority.get(item.kind, 999)))
    kept: list[PiiMatch] = []
    for item in found:
        if item.start < 0 or item.end > len(text) or item.start >= item.end:
            continue
        if not kept or item.start >= kept[-1].end:
            kept.append(item)
    return kept


def apply_policy(text: str, actions: dict) -> tuple[str, dict, list]:
    """Return (sanitized_text, {kind: count_masked}, [unique_blocked_kinds]).

    An unknown or invalid action fails safe to ``mask`` for recognized PII;
    callers should validate admin policies before they reach this function.
    """
    masked: dict[str, int] = {}
    blocked: list[str] = []
    out = text
    valid_actions = {"allow", "mask", "block"}
    for match in reversed(find_pii(text)):
        action = actions.get(match.kind, DEFAULT_ACTIONS.get(match.kind, "mask"))
        if action not in valid_actions:
            action = "mask"
        if action == "mask":
            out = out[:match.start] + f"[{match.kind.upper()}]" + out[match.end:]
            masked[match.kind] = masked.get(match.kind, 0) + 1
        elif action == "block" and match.kind not in blocked:
            blocked.append(match.kind)
    return out, masked, sorted(blocked)
