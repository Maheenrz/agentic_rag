"""
pii.py
-------
Finds personal / financial identifiers in text and applies a per-entity
policy: allow | mask | block. Used on the user's QUESTION (so a card number is
never sent to Groq, cached, or written to the chat history) and on the ANSWER.

Why per-entity policy: a company handbook legitimately contains staff emails
and phone numbers (allow), but nobody should be pasting card numbers or national
ID numbers into a chat (mask). The admin decides, per organisation.

Detection is regex + checksum (Luhn for cards, mod-97 for IBAN), which keeps
false positives low: an order number like 4111-1111-1111-1112 that fails Luhn is
left alone. It is not a full PII engine (no names/addresses); for that, industry
uses NER models such as Microsoft Presidio -- see the notes in the reply.
"""

import re
from dataclasses import dataclass

KINDS = ("card", "cnic", "iban", "ssn", "email", "phone", "ip")
DEFAULT_ACTIONS = {"card": "mask", "cnic": "mask", "iban": "mask", "ssn": "mask",
                   "email": "allow", "phone": "allow", "ip": "allow"}


@dataclass
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
    if not 15 <= len(s) <= 34:
        return False
    s = s[4:] + s[:4]
    return int("".join(str(int(c, 36)) for c in s)) % 97 == 1


_CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
_CNIC_DASHED = re.compile(r"(?<!\d)\d{5}-\d{7}-\d(?!\d)")
_CNIC_CONTEXT = re.compile(r"(?i)\b(?:cnic|nic|id\s*card)\b\D{0,15}(\d{13})(?!\d)")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")
_SSN = re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_PHONE_PK = re.compile(r"(?<![\d+A-Za-z-])(?:\+92|0092|0)[-\s]?3\d{2}[-\s]?\d{7}(?!\d)")
_PHONE_INTL = re.compile(r"(?<![\d+])\+\d{1,3}[\s.-]?\(?\d{2,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{3,4}(?!\d)")
_IPV4 = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?![\d.])")

_PRIORITY = {k: i for i, k in enumerate(("iban", "card", "cnic", "ssn", "email", "phone", "ip"))}


def find_pii(text: str) -> list[PiiMatch]:
    found: list[PiiMatch] = []
    for m in _IBAN.finditer(text):
        if _iban_ok(m.group(0)):
            found.append(PiiMatch("iban", m.start(), m.end()))
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and len(set(digits)) > 1 and _luhn(digits):      # all-same-digit runs pass Luhn but aren't cards
            found.append(PiiMatch("card", m.start(), m.end()))
    for m in _CNIC_DASHED.finditer(text):
        found.append(PiiMatch("cnic", m.start(), m.end()))
    for m in _CNIC_CONTEXT.finditer(text):
        found.append(PiiMatch("cnic", m.start(1), m.end(1)))
    for m in _SSN.finditer(text):
        found.append(PiiMatch("ssn", m.start(), m.end()))
    for m in _EMAIL.finditer(text):
        found.append(PiiMatch("email", m.start(), m.end()))
    for rx in (_PHONE_PK, _PHONE_INTL):
        for m in rx.finditer(text):
            found.append(PiiMatch("phone", m.start(), m.end()))
    for m in _IPV4.finditer(text):
        found.append(PiiMatch("ip", m.start(), m.end()))

    found.sort(key=lambda p: (p.start, _PRIORITY[p.kind]))
    kept: list[PiiMatch] = []
    for p in found:                                   # drop overlaps (e.g. a card also looking like a phone)
        if not kept or p.start >= kept[-1].end:
            kept.append(p)
    return kept


def apply_policy(text: str, actions: dict) -> tuple[str, dict, list]:
    """Returns (new_text, {kind: count_masked}, [kinds_that_must_block])."""
    masked: dict[str, int] = {}
    blocked: list[str] = []
    out = text
    for p in reversed(find_pii(text)):                # right-to-left so earlier offsets stay valid
        action = actions.get(p.kind, "allow")
        if action == "mask":
            out = out[:p.start] + f"[{p.kind.upper()}]" + out[p.end:]
            masked[p.kind] = masked.get(p.kind, 0) + 1
        elif action == "block" and p.kind not in blocked:
            blocked.append(p.kind)
    return out, masked, sorted(blocked)