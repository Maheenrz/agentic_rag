import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { Send, Sparkles, X, ShieldCheck, Lock, Upload, FileText, Loader2 } from 'lucide-react';
import { Button } from './ui';
import { guestUpload, guestStreamChat, guestRelease } from '../lib/api';

const ACCEPT = '.pdf,.txt,.md,.docx,.html,.htm,.xlsx';
const GUEST_QUESTION_CAP = 2;   // keep in sync with GUEST_QUESTION_LIMIT_CALLS on the server

export default function GuestChat({ open, onClose, question }) {
  const [phase, setPhase] = useState('upload');         // 'upload' | 'chat'
  const [files, setFiles] = useState([]);
  const [session, setSession] = useState(null);          // {collection_id, chunks, ttl_minutes}
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [asked, setAsked] = useState(0);                 // how many guest questions have been sent

  const bottom = useRef(null);
  const fileInput = useRef(null);
  const seeded = useRef(false);
  const abortRef = useRef(null);                         // holds the AbortController for the active stream

  // ---- On close: abort the stream, release the server collection, reset state ----
  useEffect(() => {
    if (open) return;
    abortRef.current?.abort();                           // ← kill any in-flight stream
    abortRef.current = null;
    if (session?.collection_id) guestRelease(session.collection_id);
    setPhase('upload');
    setFiles([]);
    setSession(null);
    setMessages([]);
    setInput('');
    setError('');
    setAsked(0);
    seeded.current = false;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Auto-scroll to the newest message
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages]);

  // Seed the composer with a question typed on the landing page (if any)
  useEffect(() => {
    if (!open || !question || seeded.current) return;
    seeded.current = true;
    setInput(question);
  }, [open, question]);

  function patchLast(fn) {
    setMessages((ms) => {
      const next = [...ms];
      next[next.length - 1] = fn(next[next.length - 1]);
      return next;
    });
  }

  // ---- Step 1: upload the document ----
  async function upload() {
    if (!files.length) return setError('Choose a document first.');
    setBusy(true);
    setError('');
    try {
      const data = await guestUpload(files);
      setSession(data);
      setPhase('chat');
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  // ---- Step 2: ask a question ----
  async function ask(e) {
    e?.preventDefault();
    const text = input.trim();
    if (!text || busy || !session) return;

    setInput('');
    setError('');
    setMessages((m) => [...m, { role: 'user', content: text }]);
    setBusy(true);
    setAsked((n) => n + 1);
    setMessages((m) => [...m, { role: 'assistant', content: '', sources: [], trace: [], pending: true }]);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await guestStreamChat(
        session.collection_id,
        text,
        (ev) => {
          if (ev.type === 'trace')      patchLast((m) => ({ ...m, trace: ev.trace }));
          else if (ev.type === 'reset') patchLast((m) => ({ ...m, content: '' }));
          else if (ev.type === 'token') patchLast((m) => ({ ...m, content: m.content + ev.text }));
          else if (ev.type === 'final') patchLast((m) => ({ ...m, content: ev.answer, sources: ev.sources, trace: ev.trace, pending: false }));
          else if (ev.type === 'error') throw new Error(ev.detail);
        },
        controller.signal
      );
    } catch (err) {
      // AbortError is expected when the user closes the modal — don't show it.
      if (err.name !== 'AbortError') setError(err.message);
      patchLast((m) => ({ ...m, pending: false }));
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setBusy(false);
    }
  }

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[120] grid place-items-center p-4">
      <div className="absolute inset-0 bg-black/70 backdrop-blur-md" onClick={onClose} />

      <div className="glass-strong relative flex h-[80vh] w-full max-w-3xl flex-col overflow-hidden rounded-3xl animate-fade-up">
        {/* Header */}
        <header className="flex items-center gap-3 border-b border-border px-5 py-4">
          <div className="grid h-8 w-8 place-items-center rounded-xl bg-accent/15 text-accent">
            <Sparkles size={16} />
          </div>
          <div className="flex-1">
            <div className="font-display text-xl leading-none">Try Docs Assistant</div>
            <div className="mt-1 flex items-center gap-1.5 text-xs text-muted">
              <ShieldCheck size={12} /> Guest mode — your file stays private and is deleted automatically
            </div>
          </div>
          <button onClick={onClose} className="rounded-full p-2 text-muted hover:bg-surface-2 hover:text-ink">
            <X size={16} />
          </button>
        </header>

        {/* ---------- Upload step ---------- */}
        {phase === 'upload' && (
          <div className="flex-1 overflow-y-auto px-6 py-6">
            <div className="mx-auto max-w-md space-y-5 pt-4 text-center">
              <div className="mx-auto grid h-14 w-14 place-items-center rounded-2xl glass text-accent">
                <Upload size={22} strokeWidth={1.75} />
              </div>
              <h2 className="font-display text-3xl leading-tight">Upload a document to try it</h2>
              <p className="text-sm text-muted">
                PDF, Word, Excel, HTML, Markdown, or text. We'll answer questions about it — nothing is saved, and it's deleted when you close this window.
              </p>

              <div
                onClick={() => fileInput.current?.click()}
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => { e.preventDefault(); setFiles([...e.dataTransfer.files]); }}
                className="glass mt-2 cursor-pointer rounded-2xl border border-dashed border-border px-6 py-8 transition hover:border-accent"
              >
                <input
                  ref={fileInput}
                  type="file"
                  accept={ACCEPT}
                  className="hidden"
                  onChange={(e) => setFiles([...e.target.files])}
                />
                {files.length === 0 ? (
                  <p className="text-sm text-muted">Click to choose, or drag a file here</p>
                ) : (
                  <div className="flex flex-col items-center gap-2">
                    {files.map((f, i) => (
                      <div key={i} className="flex items-center gap-2 text-sm text-ink">
                        <FileText size={14} className="text-accent" />
                        <span className="truncate max-w-[280px]">{f.name}</span>
                        <span className="text-xs text-muted">({Math.round(f.size / 1024)} KB)</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <Button
                variant="primary"
                onClick={upload}
                disabled={busy || !files.length}
                className="w-full py-3"
              >
                {busy ? <><Loader2 size={15} className="mr-2 animate-spin" /> Indexing…</> : 'Start asking'}
              </Button>

              {error && <p className="text-sm text-danger">{error}</p>}
            </div>
          </div>
        )}

        {/* ---------- Chat step ---------- */}
        {phase === 'chat' && (
          <>
            <div className="flex-1 space-y-5 overflow-y-auto px-6 py-6">
              <div className="flex items-center gap-2 rounded-xl border border-border bg-surface/60 px-3 py-2 text-xs text-muted">
                <FileText size={12} className="text-accent" />
                <span className="truncate">{files[0]?.name}</span>
                <span>· {session?.chunks} chunks indexed</span>
              </div>

              {messages.map((m, i) => (
                <div key={i}>
                  {m.role === 'user' ? (
                    <div className="ml-auto max-w-[78%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-surface-2 px-4 py-3 text-sm">
                      {m.content}
                    </div>
                  ) : (
                    <div className="max-w-[85%] space-y-3">
                      <div className="whitespace-pre-wrap text-sm leading-relaxed text-ink">
                        {m.content || (m.pending ? <span className="animate-pulse-soft text-muted">Thinking…</span> : '')}
                      </div>
                      {m.sources?.length > 0 && (
                        <div className="space-y-2">
                          {m.sources.map((s, si) => (
                            <div key={si} className="glass rounded-xl px-4 py-3 text-sm">
                              <div className="text-xs text-muted">
                                {s.source}{s.location ? ` · ${s.location.split(', ').slice(1).join(', ')}` : ''}
                              </div>
                              {s.quote && (
                                <details className="mt-1.5">
                                  <summary className="cursor-pointer text-xs text-muted transition hover:text-ink">Show quote</summary>
                                  <p className="mt-1.5 border-l-2 border-[color:var(--color-blue)] pl-3 text-sm italic text-ink/90">
                                    “{s.quote}”
                                  </p>
                                </details>
                              )}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              ))}
              <div ref={bottom} />
            </div>

            {/* Sign-in nudge, once at least one assistant answer finished */}
            {messages.some((m) => m.role === 'assistant' && !m.pending && m.content) && asked < GUEST_QUESTION_CAP && (
              <div className="border-t border-border bg-surface-2/40 px-6 py-3 text-sm">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2 text-muted">
                    <Lock size={13} />
                    Sign in to keep chats, upload many docs, and search your team's library.
                  </div>
                  <div className="flex gap-2">
                    <Link to="/register" className="rounded-full bg-accent px-3.5 py-1.5 text-xs font-semibold text-canvas transition hover:bg-accent-hover">
                      Create an organisation
                    </Link>
                    <Link to="/login" className="rounded-full border border-border px-3.5 py-1.5 text-xs text-ink transition hover:bg-surface-2">
                      Sign in
                    </Link>
                  </div>
                </div>
              </div>
            )}

            {error && <p className="px-6 pb-2 text-sm text-danger">{error}</p>}

            {/* Composer — replaced by a sign-in card once the guest hits the cap */}
            {asked < GUEST_QUESTION_CAP ? (
              <form onSubmit={ask} className="flex items-end gap-3 border-t border-border bg-black/20 p-4">
                <textarea
                  rows={2}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(); } }}
                  placeholder={`Ask anything about the document… (${GUEST_QUESTION_CAP - asked} left)`}
                  className="flex-1 resize-none rounded-2xl border border-border bg-surface-2 px-4 py-3 text-sm placeholder:text-faint focus:border-accent focus:outline-none"
                />
                <Button variant="primary" type="submit" disabled={busy || !input.trim()} size="icon" className="!h-11 !w-11">
                  <Send size={16} strokeWidth={2.25} />
                </Button>
              </form>
            ) : (
              <div className="border-t border-border bg-surface-2/60 p-5">
                <div className="mx-auto max-w-md text-center">
                  <div className="mx-auto mb-3 grid h-11 w-11 place-items-center rounded-2xl glass text-accent">
                    <Lock size={18} />
                  </div>
                  <h3 className="font-display text-2xl leading-tight">You've used your free questions</h3>
                  <p className="mt-2 text-sm text-muted">
                    Sign in to keep asking, save your chats, upload many documents, and search your team's library.
                  </p>
                  <div className="mt-5 flex justify-center gap-2">
                    <Link to="/register" className="rounded-full bg-accent px-5 py-2.5 text-sm font-semibold text-canvas transition hover:bg-accent-hover">
                      Create an organisation
                    </Link>
                    <Link to="/login" className="rounded-full border border-border px-5 py-2.5 text-sm text-ink transition hover:bg-surface-2">
                      Sign in
                    </Link>
                  </div>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}