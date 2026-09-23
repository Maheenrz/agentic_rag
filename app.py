"""
app.py
------
Streamlit UI for the Agentic RAG project.

Session vs thread: st.session_state is just "what's on screen in this
browser tab right now" -- it resets on refresh. A "thread" is a persisted,
resumable conversation (session_store.py, SQLite) with its own id, its own
document collection (document_processor.py, Chroma), and its own place in
the History list below. "New chat" finalizes the current thread (saves a
summary into cross-thread conversation memory) and starts a fresh one.
"""

import html
import time

import streamlit as st

from config import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_TOP_K,
    GENERATION_MODEL,
    logger,
)
from document_processor import (
    add_documents_to_thread,
    chunk_documents,
    get_embedding_mode_label,
    get_thread_vector_store,
    load_uploaded_documents,
    save_thread_summary,
    thread_document_count,
)
from memory_manager import ConversationMemory
from rag_engine import RAGEngine
from session_store import (
    add_message,
    create_thread,
    get_messages,
    list_threads,
    rename_thread,
    thread_message_count,
)

BRAND_EMOJI = "🦾"
AGENT_EMOJI = "🤖"

st.set_page_config(page_title="Agentic RAG", page_icon=BRAND_EMOJI, layout="wide")

# --------------------------------------------------------------------------
# Dark theme, orange accent
# --------------------------------------------------------------------------
BG = "#141210"
PANEL = "#1D1A17"
PANEL_2 = "#262220"
BORDER = "rgba(232, 130, 91, 0.22)"
ORANGE = "#E8825B"
ORANGE_DIM = "rgba(232, 130, 91, 0.14)"
TEXT = "#FFFFFF"
TEXT_DIM = "#A79C92"

st.markdown(f"""
<style>
    #MainMenu, footer {{visibility: hidden;}}
    header[data-testid="stHeader"] {{background: {BG} !important;}}
    html, body, .stApp {{background-color: {BG} !important; color: {TEXT} !important;}}
    .stApp p, .stApp span, .stApp label, .stApp li, .stApp div {{color: {TEXT};}}
    :root, .stApp {{
        --primary-color: {ORANGE};
        --background-color: {BG};
        --secondary-background-color: {PANEL};
        --text-color: {TEXT};
    }}
    section[data-testid="stSidebar"] {{background-color: {PANEL}; border-right: 1px solid {BORDER};}}
    section[data-testid="stSidebar"] * {{color: {TEXT};}}
    button[data-testid="stSidebarCollapseButton"] svg,
    button[data-testid="baseButton-headerNoPadding"] svg {{fill: {TEXT} !important; color: {TEXT} !important;}}
    [data-testid="stWidgetLabel"] p, [data-testid="stWidgetLabel"] label {{color: {TEXT} !important;}}
    div[data-testid="stChatInput"] {{background-color: {PANEL} !important; border: 1px solid {BORDER}; border-radius: 14px;}}
    div[data-testid="stChatInput"] textarea {{color: {TEXT} !important; background-color: {PANEL} !important;}}
    div[data-testid="stChatInput"] textarea::placeholder {{color: {TEXT_DIM} !important;}}
    div[data-testid="stChatInput"] button {{background-color: {ORANGE} !important; color: #1A1512 !important; border: none !important;}}
    div[data-testid="stChatInput"] button svg {{fill: #1A1512 !important;}}
    .stChatMessage {{background-color: {PANEL} !important; border: 1px solid {BORDER}; border-radius: 14px; padding: 10px 14px; margin-bottom: 6px;}}
    .stChatMessage p, .stChatMessage li, .stChatMessage span {{color: {TEXT} !important;}}
    .stButton>button {{background-color: {PANEL_2}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 10px;}}
    .stButton>button:hover {{border-color: {ORANGE}; color: {ORANGE};}}
    .stButton>button[kind="primary"] {{background-color: {ORANGE}; color: #1A1512; border: none;}}
    .stButton>button[kind="primary"]:hover {{color: #1A1512; filter: brightness(1.08);}}
    .stButton>button:disabled {{background-color: {PANEL_2} !important; color: {TEXT_DIM} !important; border-color: {BORDER} !important; opacity: 0.6;}}
    div[data-testid="stStatusWidget"], div[data-testid="stExpander"] {{background-color: {PANEL}; border: 1px solid {BORDER}; border-radius: 12px;}}
    div[data-testid="stExpander"] summary, div[data-testid="stExpander"] summary * {{color: {TEXT} !important; background-color: transparent !important;}}
    details[data-testid="stExpander"] > summary:hover {{color: {ORANGE} !important;}}
    div[data-testid="stFileUploaderDropzone"] {{background-color: {PANEL_2} !important; border: 1px dashed {BORDER};}}
    div[data-testid="stFileUploaderDropzone"] * {{color: {TEXT} !important;}}
    div[data-testid="stAlert"] {{background-color: {PANEL} !important; border: 1px solid {BORDER} !important; color: {TEXT} !important;}}
    div[data-testid="stAlert"] * {{color: {TEXT} !important;}}
    div[data-testid="stSlider"] [role="slider"] {{background-color: {ORANGE} !important; border-color: {ORANGE} !important;}}
    div[data-testid="stSlider"] [data-baseweb="slider"] div[role="progressbar"] {{background: {ORANGE} !important;}}
    .stCaption, [data-testid="stCaptionContainer"] * {{color: {TEXT_DIM} !important;}}
    .stApp code {{background-color: {PANEL_2} !important; color: {ORANGE} !important;}}
    .stApp a {{color: {ORANGE} !important;}}
    hr {{border-color: {BORDER};}}
    ::-webkit-scrollbar {{width: 10px; height: 10px;}}
    ::-webkit-scrollbar-track {{background: {BG};}}
    ::-webkit-scrollbar-thumb {{background: {PANEL_2}; border-radius: 6px; border: 2px solid {BG};}}
    ::-webkit-scrollbar-thumb:hover {{background: {ORANGE};}}

    .rag-header {{
        display: flex; align-items: center; justify-content: space-between;
        background: linear-gradient(90deg, {PANEL_2} 0%, {PANEL} 100%);
        border: 1px solid {BORDER}; border-radius: 16px; padding: 16px 22px; margin-bottom: 18px;
    }}
    .rag-header-title {{font-size: 1.8rem; font-weight: 700; color: {TEXT};}}
    .rag-badge {{
        display: inline-block; background-color: {ORANGE_DIM}; color: {ORANGE};
        border: 1px solid {BORDER}; border-radius: 999px; padding: 4px 12px;
        font-size: 0.78rem; font-weight: 600; margin-left: 8px;
    }}
    .rag-stats {{display: flex; gap: 12px; margin-bottom: 20px;}}
    .rag-stat-card {{flex: 1; background-color: {PANEL}; border: 1px solid {BORDER}; border-radius: 14px; padding: 12px 16px;}}
    .rag-stat-label {{color: {TEXT_DIM}; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em;}}
    .rag-stat-value {{color: {TEXT}; font-size: 1.05rem; font-weight: 700; margin-top: 2px;}}

    .rag-source-card {{background-color: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 12px; padding: 10px 14px; margin-bottom: 8px;}}
    .rag-source-top {{display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px;}}
    .rag-source-tool {{background-color: {ORANGE_DIM}; color: {ORANGE}; border-radius: 999px; padding: 2px 10px; font-size: 0.72rem; font-weight: 700; text-transform: uppercase;}}
    .rag-source-file {{color: {TEXT}; font-size: 0.85rem; font-weight: 600;}}
    .rag-source-snippet {{color: {TEXT_DIM}; font-size: 0.82rem; line-height: 1.4;}}

    .rag-history-row {{
        display: flex; align-items: center; justify-content: space-between;
        padding: 6px 4px; border-bottom: 1px solid {BORDER};
    }}
    .rag-history-title {{color: {TEXT}; font-size: 0.86rem;}}
    .rag-history-time {{color: {TEXT_DIM}; font-size: 0.72rem;}}
    .rag-history-current {{color: {ORANGE}; font-size: 0.86rem; font-weight: 700;}}
</style>
""", unsafe_allow_html=True)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def relative_time(ts: float) -> str:
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def render_sources(sources: list[dict]) -> None:
    if not sources:
        return
    with st.expander(f"📎 Sources ({len(sources)})"):
        for s in sources:
            tool = html.escape(s.get("tool", "unknown"))
            source_name = html.escape(str(s.get("source", "unknown")))
            page = html.escape(str(s.get("page", "?")))
            snippet = html.escape(s.get("snippet", ""))
            st.markdown(f"""
<div class="rag-source-card">
    <div class="rag-source-top">
        <span class="rag-source-tool">{tool}</span>
        <span class="rag-source-file">{source_name} · p.{page}</span>
    </div>
    <div class="rag-source-snippet">{snippet}</div>
</div>
""", unsafe_allow_html=True)


def rebuild_memory_from_messages(db_messages: list[dict]) -> ConversationMemory:
    """Reconstruct a ConversationMemory's turn history from persisted messages."""
    memory = ConversationMemory()
    pending_question = None
    for m in db_messages:
        if m["role"] == "user":
            pending_question = m["content"]
        elif m["role"] == "assistant" and pending_question is not None:
            memory.add_turn(pending_question, m["content"])
            pending_question = None
    return memory


def bind_engine_for_thread(thread_id: str, force_offline: bool) -> None:
    """
    If this thread already has indexed documents (resumed from History),
    rebuild the engine against its persistent collection automatically --
    no re-upload needed.
    """
    count = thread_document_count(thread_id, force_offline=force_offline)
    if count == 0:
        st.session_state["engine"] = None
        st.session_state["embedding_mode"] = None
        st.session_state["index_stats"] = None
        return

    vector_store = get_thread_vector_store(thread_id, force_offline=force_offline)
    retriever = vector_store.as_retriever(search_type="similarity", search_kwargs={"k": DEFAULT_TOP_K})
    st.session_state["engine"] = RAGEngine(
        retriever=retriever, top_k=DEFAULT_TOP_K, thread_id=thread_id, force_offline=force_offline,
    )
    st.session_state["embedding_mode"] = get_embedding_mode_label(force_offline=force_offline)
    st.session_state["index_stats"] = {"documents": "-", "chunks": count, "seconds": 0}


def switch_to_thread(thread_id: str, force_offline: bool) -> None:
    st.session_state["thread_id"] = thread_id
    db_messages = get_messages(thread_id)
    st.session_state["messages"] = db_messages
    st.session_state["memory"] = rebuild_memory_from_messages(db_messages)
    st.session_state["last_response_time"] = None
    bind_engine_for_thread(thread_id, force_offline)


def finalize_thread(thread_id: str, force_offline: bool) -> None:
    """Called before abandoning a thread (New chat) -- saves it into
    cross-thread conversation memory and gives it a real title."""
    memory: ConversationMemory = st.session_state["memory"]
    title, summary = memory.summarize_thread_for_storage()
    if summary:
        save_thread_summary(thread_id, title or "Untitled chat", summary, force_offline=force_offline)
    if title:
        rename_thread(thread_id, title)


# --------------------------------------------------------------------------
# Session state / thread bootstrap
# --------------------------------------------------------------------------
def init_session_state() -> None:
    if "thread_id" not in st.session_state:
        st.session_state["thread_id"] = create_thread()
    if "messages" not in st.session_state:
        st.session_state["messages"] = []
    if "memory" not in st.session_state:
        st.session_state["memory"] = ConversationMemory()
    if "engine" not in st.session_state:
        st.session_state["engine"] = None
    if "index_stats" not in st.session_state:
        st.session_state["index_stats"] = None
    if "embedding_mode" not in st.session_state:
        st.session_state["embedding_mode"] = None
    if "last_response_time" not in st.session_state:
        st.session_state["last_response_time"] = None


init_session_state()


# --------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------
with st.sidebar:
    st.markdown(f"### {BRAND_EMOJI} Agentic RAG")

    # Captured here, but handled further down once force_offline exists --
    # Streamlit widgets return their value at the point they're declared,
    # but nothing stops us from acting on it later in the same run.
    new_chat_clicked = st.button("🗨️ New chat", use_container_width=True)

    st.divider()
    st.markdown("**Documents**")
    uploaded_files = st.file_uploader(
        "Upload PDF or TXT files (add more anytime -- they're appended)",
        type=["pdf", "txt"], accept_multiple_files=True, label_visibility="collapsed",
    )

    force_offline = st.checkbox(
        "Skip HuggingFace (offline embeddings)",
        value=False,
        help="Enable if HuggingFace is blocked/rate-limited. Uses a local "
             "hashing embedder instead -- no network call, but less "
             "semantically accurate than the real model.",
    )

    with st.expander("⚙️ Chunking & retrieval settings"):
        chunk_size = st.slider("Chunk size", 200, 2000, DEFAULT_CHUNK_SIZE, step=50)
        chunk_overlap = st.slider("Chunk overlap", 0, 300, DEFAULT_CHUNK_OVERLAP, step=10)
        top_k = st.slider("Top K chunks", 1, 10, DEFAULT_TOP_K)

    # ---- Now that force_offline exists, handle the New chat click ----
    if new_chat_clicked:
        if st.session_state["messages"]:
            finalize_thread(st.session_state["thread_id"], force_offline)
        new_id = create_thread()
        st.session_state["thread_id"] = new_id
        st.session_state["messages"] = []
        st.session_state["memory"] = ConversationMemory()
        st.session_state["engine"] = None
        st.session_state["index_stats"] = None
        st.session_state["embedding_mode"] = None
        st.session_state["last_response_time"] = None
        st.rerun()

    if st.button("🔨 Add to index", use_container_width=True,
                 disabled=not uploaded_files, type="primary"):
        with st.spinner("Loading, chunking, and embedding documents..."):
            start = time.time()
            try:
                documents = load_uploaded_documents(uploaded_files)
                chunks = chunk_documents(documents, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
                vector_store = add_documents_to_thread(
                    st.session_state["thread_id"], chunks, force_offline=force_offline,
                )
                retriever = vector_store.as_retriever(
                    search_type="similarity", search_kwargs={"k": top_k}
                )
                st.session_state["engine"] = RAGEngine(
                    retriever=retriever, top_k=top_k,
                    thread_id=st.session_state["thread_id"], force_offline=force_offline,
                )
                st.session_state["embedding_mode"] = get_embedding_mode_label(force_offline=force_offline)
                total_chunks = thread_document_count(st.session_state["thread_id"], force_offline=force_offline)
                st.session_state["index_stats"] = {
                    "documents": len(documents),
                    "chunks": total_chunks,
                    "seconds": round(time.time() - start, 1),
                }
                logger.info("Index updated: %s", st.session_state["index_stats"])
            except Exception as exc:
                logger.exception("Index build failed")
                st.error(f"Failed to build index: {exc}")

    if st.session_state["index_stats"]:
        stats = st.session_state["index_stats"]
        st.success(f"This thread: {stats['chunks']} chunks indexed total")

    st.divider()
    st.markdown("**🕘 History**")
    with st.container(height=260):
        threads = list_threads(limit=25)
        if not threads:
            st.caption("No past chats yet.")
        for t in threads:
            is_current = t["thread_id"] == st.session_state["thread_id"]
            cols = st.columns([5, 2])
            title = html.escape(t["title"] or "New chat")
            when = relative_time(t["updated_at"])
            css_class = "rag-history-current" if is_current else "rag-history-title"
            cols[0].markdown(
                f'<div><span class="{css_class}">{title}</span><br>'
                f'<span class="rag-history-time">{when}</span></div>',
                unsafe_allow_html=True,
            )
            if not is_current:
                if cols[1].button("Open", key=f"open_{t['thread_id']}", use_container_width=True):
                    switch_to_thread(t["thread_id"], force_offline)
                    st.rerun()
            else:
                cols[1].markdown('<span class="rag-history-time">current</span>', unsafe_allow_html=True)

    st.divider()
    st.caption("Agentic RAG · route → (cache | memory | docs) → grade → generate → check")


# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------
online = st.session_state["engine"] is not None
status_label = "Ready" if online else "Awaiting documents"
embed_label = st.session_state["embedding_mode"] or "Not built yet"

st.markdown(f"""
<div class="rag-header">
    <div class="rag-header-title">{BRAND_EMOJI} Agentic RAG</div>
    <div>
        <span class="rag-badge">● {status_label}</span>
        <span class="rag-badge">Model: {GENERATION_MODEL}</span>
        <span class="rag-badge">Embeddings: {embed_label}</span>
    </div>
</div>
""", unsafe_allow_html=True)

stats = st.session_state["index_stats"]
last_rt = st.session_state["last_response_time"]

st.markdown(f"""
<div class="rag-stats">
    <div class="rag-stat-card">
        <div class="rag-stat-label">Chunks (this thread)</div>
        <div class="rag-stat-value">{stats['chunks'] if stats else '–'}</div>
    </div>
    <div class="rag-stat-card">
        <div class="rag-stat-label">Top K</div>
        <div class="rag-stat-value">{top_k}</div>
    </div>
    <div class="rag-stat-card">
        <div class="rag-stat-label">Last response</div>
        <div class="rag-stat-value">{f"{last_rt}s" if last_rt else '–'}</div>
    </div>
    <div class="rag-stat-card">
        <div class="rag-stat-label">Thread</div>
        <div class="rag-stat-value">{st.session_state["thread_id"][:8]}</div>
    </div>
</div>
""", unsafe_allow_html=True)


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------
if not st.session_state["messages"]:
    st.info("👋 Upload a document in the sidebar to get started, or open a past chat from History.")

for message in st.session_state["messages"]:
    avatar = AGENT_EMOJI if message["role"] == "assistant" else None
    with st.chat_message(message["role"], avatar=avatar):
        st.markdown(message["content"])
        if message.get("trace"):
            with st.expander("🧠 Agent reasoning"):
                for step in message["trace"]:
                    st.markdown(f"- {step}")
        render_sources(message.get("sources", []))

question = st.chat_input("Ask a question, or ask about a past conversation...")

if question:
    engine = st.session_state["engine"]
    thread_id = st.session_state["thread_id"]

    # A recall question ("what did we discuss...") doesn't need a
    # document-bound engine, since it searches conversation memory instead --
    # so we only block on "no engine" if this looks like it needs documents.
    # The router inside the engine actually classifies this properly; here
    # we just need *an* engine instance to run it. If none is bound yet
    # (no docs uploaded this thread), build a memory-only one on the fly.
    if engine is None:
        engine = RAGEngine(retriever=None, top_k=top_k, thread_id=thread_id, force_offline=force_offline)

    if thread_message_count(thread_id) == 0:
        rename_thread(thread_id, question[:60])

    st.session_state["messages"].append({"role": "user", "content": question})
    add_message(thread_id, "user", question)
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant", avatar=AGENT_EMOJI):
        memory = st.session_state["memory"]
        history_messages = memory.get_context_messages()
        history_text = "\n".join(f"{m['role']}: {m['content']}" for m in history_messages)

        trace: list[str] = []
        answer_placeholder = st.empty()
        streamed_text = ""
        final_state = None
        start_time = time.time()

        with st.status("Thinking...", expanded=True) as status:
            try:
                for event in engine.run_stream(question=question, history_text=history_text):
                    if event["type"] == "step":
                        trace.append(event["text"])
                        status.write(event["text"])
                    elif event["type"] == "token":
                        streamed_text += event["text"]
                        answer_placeholder.markdown(streamed_text + "▌")
                    elif event["type"] == "regenerated":
                        streamed_text = event["text"]
                        answer_placeholder.markdown(streamed_text)
                    elif event["type"] == "done":
                        final_state = event["state"]

                status.update(label="Done", state="complete", expanded=False)
            except Exception as exc:
                logger.exception("Agent run failed")
                if final_state is None and not streamed_text:
                    streamed_text = ("I couldn't retrieve any documents (none uploaded in this thread yet) "
                                      "and this doesn't look like a request to recall a past conversation. "
                                      "Try uploading a document first.")
                trace.append(f"Error: {exc}")
                status.update(label="Error", state="error", expanded=True)

        st.session_state["last_response_time"] = round(time.time() - start_time, 2)

        answer = final_state.answer if final_state else streamed_text
        sources = final_state.sources if final_state else []
        answer_placeholder.markdown(answer)

        with st.expander("🧠 Agent reasoning"):
            for step in trace:
                st.markdown(f"- {step}")
        render_sources(sources)

    st.session_state["messages"].append({"role": "assistant", "content": answer, "trace": trace, "sources": sources})
    add_message(thread_id, "assistant", answer, trace, sources)
    memory.add_turn(question=question, answer=answer)
    st.rerun()