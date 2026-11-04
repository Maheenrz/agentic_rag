
"""
quotes.py
----------
Selects a literal source quote that supports an answer.

The answer receives more weight than the question, so a sentence that merely
repeats the question is not enough. Every returned quote is verified against
the original source text. If no suitable quote exists, return an empty string.
"""

import re

MAX_QUOTE_CHARS = 320

_STOP = frozenset(
    """a an the and or but if then else of to in on at by for with from as is are
    was were be been being it its this that these those there here which who whom
    whose what when where why how not no yes do does did done can could should
    would will shall may might must have has had having i you he she we they
    them his her our your their me my us about into over under than also such
    any all each other more most some only own same so too very just per via"""
    .split()
)

_SENTENCE = re.compile(r"[^.!?\n]+(?:[.!?]+(?=\s|$)|\n|$)")
_TOKEN = re.compile(r"[a-z0-9]+(?:[.'-][a-z0-9]+)*")


def _norm(text: str) -> str:
    """Normalize whitespace without changing the words."""
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in _TOKEN.findall(text.lower())
        if token not in _STOP and (len(token) > 2 or token.isdigit())
    }


def verify_quote(quote: str, chunk_text: str) -> bool:
    """True only if the quote appears literally in the source, ignoring whitespace."""
    return bool(quote) and _norm(quote) in _norm(chunk_text)


def best_quote(
    chunk_text: str,
    answer: str,
    question: str = "",
    max_chars: int = MAX_QUOTE_CHARS,
) -> str:
    """
    Return the best short, verifiable source quote for an answer.

    Answer-token overlap is required. Question overlap is a small tie-breaker,
    not a substitute for evidence supporting the answer.
    """
    if not chunk_text or not chunk_text.strip():
        return ""

    if max_chars <= 0:
        return ""

    answer_tokens = _tokens(answer)
    question_tokens = _tokens(question)

    if not answer_tokens:
        return ""

    spans = [
        (match.start(), match.end())
        for match in _SENTENCE.finditer(chunk_text)
        if chunk_text[match.start():match.end()].strip()
    ]

    if not spans:
        return ""

    def score(index: int) -> float:
        sentence = chunk_text[spans[index][0]:spans[index][1]]
        words = _tokens(sentence)

        if not words:
            return 0.0

        answer_hits = words & answer_tokens
        # A sentence must overlap the answer. Question overlap alone cannot
        # cause an unsupported sentence to be selected.
        if not answer_hits:
            return 0.0

        question_hits = words & question_tokens
        answer_weight = sum(
            1.5 if (word.isdigit() or len(word) >= 7) else 1.0
            for word in answer_hits
        )
        question_weight = sum(
            1.5 if (word.isdigit() or len(word) >= 7) else 1.0
            for word in question_hits
            if word not in answer_tokens
        )

        return (
            answer_weight + 0.15 * question_weight
        ) / (len(words) ** 0.5)

    scores = [score(index) for index in range(len(spans))]
    best_index = max(range(len(spans)), key=lambda index: scores[index])

    if scores[best_index] <= 0:
        return ""

    start, end = spans[best_index]

    # Add a neighbouring sentence only when it also overlaps the answer and
    # the combined quote stays within the configured limit.
    if len(_norm(chunk_text[start:end])) < 80:
        for neighbour in (best_index + 1, best_index - 1):
            if not 0 <= neighbour < len(spans):
                continue
            if scores[neighbour] < 0.5 * scores[best_index]:
                continue

            new_start = min(start, spans[neighbour][0])
            new_end = max(end, spans[neighbour][1])
            candidate = _norm(chunk_text[new_start:new_end])

            if len(candidate) <= max_chars:
                start, end = new_start, new_end
                break

    quote = _norm(chunk_text[start:end])

    if len(quote) > max_chars:
        # Keep a literal contiguous source window; do not paraphrase or
        # assemble words from separate parts of the document.
        answer_hits = _tokens(answer)
        hit_position = next(
            (
                match.start()
                for match in _TOKEN.finditer(quote.lower())
                if match.group(0) in answer_hits
            ),
            0,
        )

        begin = max(0, hit_position - max_chars // 4)

        if begin:
            next_space = quote.find(" ", begin)
            if next_space != -1:
                begin = next_space + 1

        end_position = min(len(quote), begin + max_chars)
        clipped = quote[begin:end_position]

        if end_position < len(quote) and " " in clipped:
            clipped = clipped.rsplit(" ", 1)[0]

        quote = clipped

    if len(quote) > max_chars:
        quote = quote[:max_chars].rsplit(" ", 1)[0]

    return quote if verify_quote(quote, chunk_text) else ""
