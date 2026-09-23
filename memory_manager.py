"""
memory_manager.py
------------------
Conversation history management via sliding window + summary compression.

Strategy:
- Keep the last MEMORY_WINDOW_TURNS turns verbatim (full detail, most useful
  for the model right now).
- Once older turns age out of the window, compress them into a single
  running "Summary State" using a small/cheap LLM call.
- The prompt sent to the generator is always: [summary] + [raw window],
  so token cost stays roughly constant no matter how long the chat runs.
"""

from dataclasses import dataclass, field
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from config import MEMORY_WINDOW_TURNS, SMALL_MODEL, logger

_SUMMARY_PROMPT = """You are compressing a conversation history into a short \
running summary for another AI system to use as context.

Existing summary (may be empty if this is the first compression):
{existing_summary}

Older turns to fold into the summary:
{turns_to_compress}

Write an updated summary in 3-5 sentences. Preserve facts, decisions, and \
open questions. Do not include pleasantries or filler."""


@dataclass
class Turn:
    question: str
    answer: str


@dataclass
class ConversationMemory:
    """
    One instance per chat session (e.g. stored in st.session_state).
    """
    window_size: int = MEMORY_WINDOW_TURNS
    summary: str = ""
    raw_turns: list[Turn] = field(default_factory=list)
    _summarizer_llm: Optional[ChatGroq] = field(default=None, repr=False)

    def _get_summarizer(self) -> ChatGroq:
        # Lazily construct the LLM client so importing this module never
        # requires network access, and so tests can avoid it entirely.
        if self._summarizer_llm is None:
            self._summarizer_llm = ChatGroq(model=SMALL_MODEL, temperature=0)
        return self._summarizer_llm

    def add_turn(self, question: str, answer: str) -> None:
        """Record a completed exchange, then compress if the window overflowed."""
        self.raw_turns.append(Turn(question=question, answer=answer))
        logger.info("Memory: added turn (window now %d/%d)", len(self.raw_turns), self.window_size)

        if len(self.raw_turns) > self.window_size:
            self._compress_oldest()

    def _compress_oldest(self) -> None:
        """
        Fold the oldest turns (everything beyond the window) into the
        running summary, then drop them from raw_turns.
        """
        overflow_count = len(self.raw_turns) - self.window_size
        turns_to_compress = self.raw_turns[:overflow_count]
        self.raw_turns = self.raw_turns[overflow_count:]

        turns_text = "\n".join(
            f"User: {t.question}\nAssistant: {t.answer}" for t in turns_to_compress
        )

        logger.info("Memory: compressing %d aged-out turn(s) into summary", len(turns_to_compress))

        try:
            prompt = _SUMMARY_PROMPT.format(
                existing_summary=self.summary or "(none yet)",
                turns_to_compress=turns_text,
            )
            response = self._get_summarizer().invoke([
                SystemMessage(content="You compress conversation history accurately and concisely."),
                HumanMessage(content=prompt),
            ])
            self.summary = response.content.strip()
        except Exception:
            # Summarization is an optimization, not a critical path. If it
            # fails (rate limit, network blip), keep the old summary rather
            # than crashing the whole conversation.
            logger.exception("Memory: summarization failed, keeping previous summary")

    def get_context_messages(self) -> list[dict]:
        """
        Build the message list to prepend to the current question:
        an optional summary system message, followed by raw turns in order.
        """
        messages: list[dict] = []

        if self.summary:
            messages.append({
                "role": "system",
                "content": f"Summary of earlier conversation:\n{self.summary}",
            })

        for turn in self.raw_turns:
            messages.append({"role": "user", "content": turn.question})
            messages.append({"role": "assistant", "content": turn.answer})

        return messages

    def reset(self) -> None:
        """Used by the sidebar 'New Chat' button."""
        logger.info("Memory: reset for new chat")
        self.summary = ""
        self.raw_turns = []

    def summarize_thread_for_storage(self) -> tuple[str, str]:
        """
        Produce a (title, summary) pair capturing the WHOLE thread so far,
        for writing into the persistent conversation-memory store when the
        thread is finalized (New chat clicked). Different from the running
        summary above: this is a one-shot final digest of everything,
        raw turns included, not just the aged-out portion.
        """
        parts = []
        if self.summary:
            parts.append(f"Earlier summary: {self.summary}")
        for turn in self.raw_turns:
            parts.append(f"User: {turn.question}\nAssistant: {turn.answer}")
        transcript = "\n".join(parts)

        if not transcript.strip():
            return "", ""

        prompt = (
            "Summarize the following conversation in 2-4 sentences, capturing "
            "the key topics and conclusions. Then on a new line write a short "
            "5-8 word title for it, prefixed with \"Title: \".\n\n"
            f"Conversation:\n{transcript}"
        )

        try:
            response = self._get_summarizer().invoke([
                SystemMessage(content="You write concise, accurate conversation summaries."),
                HumanMessage(content=prompt),
            ])
            text = response.content.strip()
        except Exception:
            logger.exception("Thread finalization summary failed")
            return "Untitled chat", transcript[:300]

        title = "Untitled chat"
        summary_lines = []
        for line in text.splitlines():
            if line.strip().lower().startswith("title:"):
                title = line.split(":", 1)[1].strip()
            else:
                summary_lines.append(line)

        summary = "\n".join(summary_lines).strip() or text
        return title, summary