"""
quotes.py
----------
Picks the exact sentence(s) in a source chunk that back up an answer, so the
user can see the real words from the document next to the citation.

How it works (no LLM call, so it is free and cannot be prompt-injected):
  1. Split the chunk into sentences, remembering where each one starts and ends.
  2. Score each sentence by how many meaningful words it shares with the
     answer (plus the question). Numbers and rare-looking words count more.
  3. Take the best sentence, add the next one if it also matches and there is room.
  4. Cut the quote straight out of the ORIGINAL text with those positions.

Because the quote is sliced from the chunk, it is word-for-word real by
construction. verify_quote() double-checks this anyway before we return it.
If nothing in the chunk overlaps with the answer we return "" -- showing no
quote is better than showing a misleading one.

Limit: this is "most similar sentence", not "the sentence the model actually
used". For extractive answers (policies, definitions, numbers) that is almost
always the same sentence. Upgrade later: ask a model for the quote, then keep it
only if verify_quote() passes.
"""

import re

MAX_QUOTE_CHARS = 320

_STOP = frozenset("""a an the and or but if then else of to in on at by for with from as is are was were be been being
it its this that these those there here which who whom whose what when where why how not no yes do does did done can could
should would will shall may might must have has had having i you he she we they them his her our your their me my us
about into over under than also such any all each other more most some only own same so too very just per via""".split())

_SENTENCE = re.compile(r"[^.!?\n]+(?:[.!?]+(?=\s|$)|\n|$)")
_TOKEN = re.compile(r"[a-z0-9]+(?:[.'-][a-z0-9]+)*")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if t not in _STOP and (len(t) > 2 or t.isdigit())}


def verify_quote(quote: str, chunk_text: str) -> bool:
    """True only if the quote appears in the chunk word-for-word (ignoring whitespace differences)."""
    return bool(quote) and _norm(quote) in _norm(chunk_text)


def best_quote(chunk_text: str, answer: str, question: str = "", max_chars: int = MAX_QUOTE_CHARS) -> str:
    wanted = _tokens(answer) | _tokens(question)
    if not wanted or not chunk_text.strip():
        return ""

    spans = [(m.start(), m.end()) for m in _SENTENCE.finditer(chunk_text) if chunk_text[m.start():m.end()].strip()]
    if not spans:
        return ""

    def score(i: int) -> float:
        words = _tokens(chunk_text[spans[i][0]:spans[i][1]])
        if not words:
            return 0.0
        hits = words & wanted
        weight = sum(1.5 if (w.isdigit() or len(w) >= 7) else 1.0 for w in hits)
        return weight / (len(words) ** 0.5)           # long rambling sentences must earn their length

    scores = [score(i) for i in range(len(spans))]
    best = max(range(len(spans)), key=lambda i: scores[i])
    if scores[best] <= 0:
        return ""

    start, end = spans[best]
    # a very short sentence ("See table 2.") is a weak quote: borrow the neighbour if it matches too
    for nxt in (best + 1, best - 1):
        if 0 <= nxt < len(spans) and scores[nxt] >= 0.5 * scores[best] and len(_norm(chunk_text[start:end])) < 80:
            a, b = min(start, spans[nxt][0]), max(end, spans[nxt][1])
            if len(_norm(chunk_text[a:b])) <= max_chars:
                start, end = a, b
                break

    quote = _norm(chunk_text[start:end])
    if len(quote) > max_chars:                          # too long: cut a real window around the first matching word
        hit = next((m.start() for m in _TOKEN.finditer(quote.lower()) if m.group(0) in wanted), 0)
        begin = max(0, hit - 80)
        if begin:
            begin = quote.find(" ", begin) + 1 or begin          # start on a word boundary
        quote = quote[begin:begin + max_chars].rsplit(" ", 1)[0] if begin + max_chars < len(quote) else quote[begin:]
    return quote if verify_quote(quote, chunk_text) else ""