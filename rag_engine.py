"""
rag_engine.py
-------------
The agentic core: a conditional loop (not a fixed pipeline) built from
small, single-purpose nodes:

    Router -> Retriever -> Grader -> [Query Rewriter -> Retriever -> Grader]* -> Generator -> Hallucination Checker

Each node is a plain function that takes and returns an AgentState, which
keeps the control flow easy to read, test, and later port to LangGraph's
StateGraph if persistence/checkpointing across sessions is needed -- the
node functions themselves wouldn't need to change, only how they're wired
together.

Every node appends a human-readable entry to state.trace, which the
Streamlit UI renders in the "Agent Reasoning" expander.
"""

from dataclasses import dataclass, field
from typing import Literal

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from config import GENERATION_MODEL, MAX_QUERY_REWRITES, SMALL_MODEL, logger
from document_processor import check_cache, format_context, search_memory, write_cache

NO_ANSWER_MESSAGE = "I couldn't find that information in the uploaded documents."
NO_MEMORY_ANSWER_MESSAGE = "I couldn't find that information in past conversations."
# Name of the tool that performed retrieval. Tagged explicitly on every
# state so the sources panel can show which one actually answered --
# this is also exactly where a router with more tools (web search, SQL,
# ...) would plug in without changing anything downstream.
RETRIEVAL_TOOL = "Vector DB (Chroma)"
MEMORY_TOOL = "Conversation Memory"
CACHE_TOOL = "Semantic Cache"

_GENERATION_PROMPT = """You are a document question-answering assistant.
Answer the user's question using ONLY the context below.

Rules:
1. Do not use outside knowledge.
2. If the answer is not available in the context, say exactly:
"{no_answer}"
3. Keep the answer clear and concise.
4. When useful, mention the source filename and page number.

Conversation history:
{history}

Context:
{context}

Question: {question}"""

_GRADER_PROMPT = """You are grading whether a retrieved passage is relevant \
to a user's question.

Question: {question}

Passage:
{passage}

Respond with exactly one word: RELEVANT or IRRELEVANT."""


_RECALL_GENERATION_PROMPT = """You are helping the user recall a past conversation.
Answer using ONLY the conversation summaries below.

Rules:
1. Do not use outside knowledge.
2. If the answer is not available below, say exactly:
"{no_answer}"
3. Keep the answer clear and concise.
4. Do NOT cite page numbers or filenames -- these are past conversation summaries, not documents.

Conversation history:
{history}

Past conversation summaries:
{context}

Question: {question}"""

_REWRITE_PROMPT = """The following search query returned mostly irrelevant \
results from a document search engine.

Original query: {question}

Rewrite it as a clearer, more specific search query that is more likely to \
retrieve relevant passages. Respond with ONLY the rewritten query, nothing else."""

_FAITHFULNESS_PROMPT = """Check whether the DRAFT ANSWER below is fully \
supported by the SOURCE CONTEXT. Flag any claim that is not present in the \
context.

SOURCE CONTEXT:
{context}

DRAFT ANSWER:
{answer}

Respond with exactly one word: SUPPORTED or UNSUPPORTED."""

_INTENT_PROMPT = """Classify the user's question into exactly one category.

DOCUMENT_QUESTION: should be answered using the documents uploaded in the \
current chat.
RECALL_QUESTION: asks to recall, summarize, or reference something from a \
PAST/earlier conversation or session (e.g. "what did we discuss before", \
"summarize my last chat", "what did I ask you last time", "in my other chat...").

Question: {question}

Respond with exactly one word: DOCUMENT_QUESTION or RECALL_QUESTION."""


def format_memory_context(documents: list[Document]) -> str:
    blocks = []
    for index, doc in enumerate(documents, start=1):
        title = doc.metadata.get("title", "a past conversation")
        blocks.append(f'[Past conversation {index}: "{title}"]\n{doc.page_content}')
    return "\n\n".join(blocks)


@dataclass
class AgentState:
    question: str
    history_text: str = ""
    retrieved_docs: list[Document] = field(default_factory=list)
    relevant_docs: list[Document] = field(default_factory=list)
    rewrite_count: int = 0
    answer: str = ""
    trace: list[str] = field(default_factory=list)
    tool_used: str = ""
    sources: list[dict] = field(default_factory=list)

    def log_step(self, message: str) -> None:
        self.trace.append(message)
        logger.info("Agent step: %s", message)


def build_sources(state: "AgentState") -> list[dict]:
    """
    Turn the chunks that actually fed the final answer into a de-duplicated,
    UI-ready list: which tool retrieved it, which file/page (or past thread)
    it's from, and a short snippet, so the answer can show its work.
    """
    if state.tool_used == CACHE_TOOL:
        # Cache hits already carry their sources from write_cache() time.
        return state.sources

    seen = set()
    sources: list[dict] = []

    for doc in state.relevant_docs:
        snippet = doc.page_content.strip().replace("\n", " ")
        if len(snippet) > 180:
            snippet = snippet[:180].rstrip() + "..."

        if state.tool_used == MEMORY_TOOL:
            title = doc.metadata.get("title", "Untitled chat")
            key = ("memory", title)
            if key in seen:
                continue
            seen.add(key)
            sources.append({
                "tool": MEMORY_TOOL,
                "source": title,
                "page": "-",
                "snippet": snippet,
            })
        else:
            source_name = doc.metadata.get("source", "unknown")
            page = doc.metadata.get("page", "?")
            key = (source_name, page)
            if key in seen:
                continue
            seen.add(key)
            sources.append({
                "tool": state.tool_used or RETRIEVAL_TOOL,
                "source": source_name,
                "page": page,
                "snippet": snippet,
            })

    return sources


class RAGEngine:
    """
    Wraps the LLM clients and vector-store retriever needed to run the
    agentic loop. One instance per built index (i.e. per uploaded corpus).
    """

    def __init__(self, retriever, top_k: int, thread_id: str, force_offline: bool = False) -> None:
        self.retriever = retriever
        self.top_k = top_k
        # Needed to scope the semantic cache to this thread only.
        self.thread_id = thread_id
        self.force_offline = force_offline
        # Small/cheap model for grading, rewriting, faithfulness checking,
        # and now intent classification too.
        self.small_llm = ChatGroq(model=SMALL_MODEL, temperature=0)
        # Larger model reserved for the answer the user actually reads.
        self.generation_llm = ChatGroq(model=GENERATION_MODEL, temperature=0)

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def classify_intent(self, question: str) -> str:
        """
        Router step: is this a question about the CURRENT thread's
        documents, or a request to RECALL something from a past thread?
        Defaults to DOCUMENT_QUESTION on any failure (the safer default --
        most questions are document questions).
        """
        prompt = _INTENT_PROMPT.format(question=question)
        try:
            response = self.small_llm.invoke([HumanMessage(content=prompt)])
            verdict = response.content.strip().upper()
        except Exception:
            logger.exception("Intent classification failed; defaulting to DOCUMENT_QUESTION")
            return "DOCUMENT_QUESTION"
        return "RECALL_QUESTION" if "RECALL" in verdict else "DOCUMENT_QUESTION"

    def retrieve_from_memory(self, state: AgentState) -> AgentState:
        """
        Cross-thread recall: search the GLOBAL conversation-memory store
        (every finalized thread's summary), not this thread's documents.
        """
        state.tool_used = MEMORY_TOOL
        state.retrieved_docs = search_memory(
            state.question, k=self.top_k, force_offline=self.force_offline
        )
        state.relevant_docs = state.retrieved_docs  # no grading pass for recall
        state.log_step(
            f"Retrieved {len(state.retrieved_docs)} past-thread summaries via {MEMORY_TOOL}"
        )
        return state

    def retrieve(self, state: AgentState) -> AgentState:
        state.tool_used = RETRIEVAL_TOOL
        state.retrieved_docs = self.retriever.invoke(state.question)
        state.log_step(
            f"Retrieved {len(state.retrieved_docs)} chunks via {RETRIEVAL_TOOL} "
            f"for: \"{state.question}\""
        )
        return state

    def grade_chunks(self, state: AgentState) -> AgentState:
        relevant = []
        failures = 0
        for doc in state.retrieved_docs:
            prompt = _GRADER_PROMPT.format(question=state.question, passage=doc.page_content)
            try:
                response = self.small_llm.invoke([HumanMessage(content=prompt)])
                verdict = response.content.strip().upper()
            except Exception:
                failures += 1
                logger.exception("Grading call failed")
                verdict = "RELEVANT"

            if "RELEVANT" in verdict and "IRRELEVANT" not in verdict:
                relevant.append(doc)

        state.relevant_docs = relevant
        msg = f"Graded chunks: {len(relevant)}/{len(state.retrieved_docs)} relevant"
        if failures:
            msg += f" — {failures} grading call(s) FAILED and fail-opened (check rate limits)"
        state.log_step(msg)
        return state
    
    def rewrite_query(self, state: AgentState) -> AgentState:
        prompt = _REWRITE_PROMPT.format(question=state.question)
        try:
            response = self.small_llm.invoke([HumanMessage(content=prompt)])
            new_question = response.content.strip()
        except Exception:
            logger.exception("Query rewrite failed; keeping original question")
            state.log_step(f"Rewrite failed, keeping original query: \"{state.question}\"")
            state.rewrite_count += 1
            return state

        state.log_step(f"Rewrote query: \"{state.question}\" -> \"{new_question}\"")
        state.question = new_question
        state.rewrite_count += 1
        return state


    def _build_context_and_prompt(self, state: AgentState, extra: str = "") -> tuple[str, str]:
        if state.tool_used == MEMORY_TOOL:
            context = format_memory_context(state.relevant_docs)
            template = _RECALL_GENERATION_PROMPT
            no_answer = NO_MEMORY_ANSWER_MESSAGE
        else:
            context = format_context(state.relevant_docs)
            template = _GENERATION_PROMPT
            no_answer = NO_ANSWER_MESSAGE

        prompt = template.format(
            no_answer=no_answer,
            history=state.history_text or "(none)",
            context=context,
            question=state.question,
        ) + extra
        return context, prompt
    
    def generate(self, state: AgentState) -> AgentState:
        _, prompt = self._build_context_and_prompt(state)
        response = self.generation_llm.invoke([
            SystemMessage(content="You answer strictly from the given context."),
            HumanMessage(content=prompt),
        ])
        state.answer = response.content.strip()
        state.log_step("Generated draft answer")
        return state

    def generate_stream(self, state: AgentState):
        _, prompt = self._build_context_and_prompt(state)
        messages = [
            SystemMessage(content="You answer strictly from the given context."),
            HumanMessage(content=prompt),
        ]
        full_answer = ""
        for chunk in self.generation_llm.stream(messages):
            piece = chunk.content or ""
            if piece:
                full_answer += piece
                yield piece
        state.answer = full_answer.strip()
        state.log_step("Generated draft answer (streamed)")

    def check_faithfulness(self, state: AgentState) -> AgentState:
        context, _ = self._build_context_and_prompt(state)
        prompt = _FAITHFULNESS_PROMPT.format(context=context, answer=state.answer)
        try:
            response = self.small_llm.invoke([HumanMessage(content=prompt)])
            verdict = response.content.strip().upper()
        except Exception:
            logger.exception("Faithfulness check failed; keeping draft answer")
            verdict = "SUPPORTED"

        if "UNSUPPORTED" in verdict:
            state.log_step("Faithfulness check: UNSUPPORTED -> regenerating with stricter constraint")
            _, strict_prompt = self._build_context_and_prompt(
                state, extra="\n\nBe extremely conservative: only state facts explicitly present in the context."
            )
            response = self.generation_llm.invoke([HumanMessage(content=strict_prompt)])
            state.answer = response.content.strip()
        else:
            state.log_step("Faithfulness check: PASSED")

        return state
    # ------------------------------------------------------------------
    # Control loop
    # ------------------------------------------------------------------

    def run(self, question: str, history_text: str = "") -> AgentState:
        """
        Router: RECALL_QUESTION -> conversation memory (cross-thread).
                DOCUMENT_QUESTION -> cache check, then
                    retrieve -> grade -> (rewrite -> retrieve -> grade)* ->
                    generate -> check -> write to cache.
        """
        state = AgentState(question=question, history_text=history_text)

        intent = self.classify_intent(question)
        state.log_step(f"Router: classified as {intent}")

        if intent == "RECALL_QUESTION":
            state = self.retrieve_from_memory(state)
            if not state.relevant_docs:
                state.answer = "I don't have any past conversations to recall from yet."
                state.log_step("No past conversation memory found")
                state.sources = build_sources(state)
                return state
            state = self.generate(state)
            state = self.check_faithfulness(state)
            state.sources = build_sources(state)
            return state

        if self.retriever is None:
            state.answer = (
                "No documents are indexed in this chat yet. Upload a PDF or "
                "TXT file in the sidebar, or ask me to recall a past conversation."
            )
            state.log_step("No retriever bound for this thread -- no documents indexed yet")
            return state

        cached = check_cache(question, thread_id=self.thread_id, force_offline=self.force_offline)
        if cached:
            state.answer = cached["answer"]
            state.sources = cached["sources"]
            state.tool_used = CACHE_TOOL
            state.log_step(f"Semantic cache hit (similarity={cached['similarity']}) -- skipped generation")
            return state

        state = self.retrieve(state)
        state = self.grade_chunks(state)

        while not state.relevant_docs and state.rewrite_count < MAX_QUERY_REWRITES:
            state = self.rewrite_query(state)
            state = self.retrieve(state)
            state = self.grade_chunks(state)

        if not state.relevant_docs:
            state.answer = NO_ANSWER_MESSAGE
            state.log_step("No relevant chunks after retries -> returning fallback answer")
            state.sources = build_sources(state)
            return state

        state = self.generate(state)
        state = self.check_faithfulness(state)
        state.sources = build_sources(state)

        if state.answer != NO_ANSWER_MESSAGE:
            write_cache(question, state.answer, state.sources, thread_id=self.thread_id, force_offline=self.force_offline)

        return state

    def run_stream(self, question: str, history_text: str = ""):
        """
        Same routing as run(), but yields events as they happen so the UI
        can show live progress instead of a single blocking spinner:

            {"type": "step",  "text": <reasoning step>}    -- retrieve/grade/route/etc.
            {"type": "token", "text": <answer chunk>}       -- streamed generation
            {"type": "done",  "state": <final AgentState>}  -- always the last event
        """
        state = AgentState(question=question, history_text=history_text)

        intent = self.classify_intent(question)
        state.log_step(f"Router: classified as {intent}")
        yield {"type": "step", "text": state.trace[-1]}

        # ---------------- RECALL path: search past threads ----------------
        if intent == "RECALL_QUESTION":
            state = self.retrieve_from_memory(state)
            yield {"type": "step", "text": state.trace[-1]}

            if not state.relevant_docs:
                state.answer = "I don't have any past conversations to recall from yet."
                state.log_step("No past conversation memory found")
                yield {"type": "step", "text": state.trace[-1]}
                state.sources = build_sources(state)
                yield {"type": "done", "state": state}
                return

            for piece in self.generate_stream(state):
                yield {"type": "token", "text": piece}
            yield {"type": "step", "text": state.trace[-1]}

            state = self.check_faithfulness(state)
            yield {"type": "step", "text": state.trace[-1]}
            if state.trace[-1] != "Faithfulness check: PASSED":
                yield {"type": "regenerated", "text": state.answer}

            state.sources = build_sources(state)
            yield {"type": "done", "state": state}
            return

        # ---------------- DOCUMENT path: cache check first ----------------
        if self.retriever is None:
            state.answer = (
                "No documents are indexed in this chat yet. Upload a PDF or "
                "TXT file in the sidebar, or ask me to recall a past conversation."
            )
            state.log_step("No retriever bound for this thread -- no documents indexed yet")
            yield {"type": "step", "text": state.trace[-1]}
            yield {"type": "token", "text": state.answer}
            yield {"type": "done", "state": state}
            return

        cached = check_cache(question, thread_id=self.thread_id, force_offline=self.force_offline)
        if cached:
            state.answer = cached["answer"]
            state.sources = cached["sources"]
            state.tool_used = CACHE_TOOL
            state.log_step(f"Semantic cache hit (similarity={cached['similarity']}) -- skipped generation")
            yield {"type": "step", "text": state.trace[-1]}
            yield {"type": "token", "text": state.answer}
            yield {"type": "done", "state": state}
            return

        state = self.retrieve(state)
        yield {"type": "step", "text": state.trace[-1]}
        state = self.grade_chunks(state)
        yield {"type": "step", "text": state.trace[-1]}

        while not state.relevant_docs and state.rewrite_count < MAX_QUERY_REWRITES:
            state = self.rewrite_query(state)
            yield {"type": "step", "text": state.trace[-1]}
            state = self.retrieve(state)
            yield {"type": "step", "text": state.trace[-1]}
            state = self.grade_chunks(state)
            yield {"type": "step", "text": state.trace[-1]}

        if not state.relevant_docs:
            state.answer = NO_ANSWER_MESSAGE
            state.log_step("No relevant chunks after retries -> returning fallback answer")
            yield {"type": "step", "text": state.trace[-1]}
            state.sources = build_sources(state)
            yield {"type": "done", "state": state}
            return

        for piece in self.generate_stream(state):
            yield {"type": "token", "text": piece}
        yield {"type": "step", "text": state.trace[-1]}

        state = self.check_faithfulness(state)
        yield {"type": "step", "text": state.trace[-1]}

        # If the faithfulness check triggered a (non-streamed) regeneration,
        # the visible answer no longer matches what was streamed above --
        # tell the UI to swap in the corrected final text.
        if state.trace[-1] == "Faithfulness check: PASSED":
            pass
        else:
            yield {"type": "regenerated", "text": state.answer}

        state.sources = build_sources(state)

        if state.answer != NO_ANSWER_MESSAGE:
            write_cache(question, state.answer, state.sources, thread_id=self.thread_id, force_offline=self.force_offline)

        yield {"type": "done", "state": state}