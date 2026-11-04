
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Send,
  Sparkles,
  X,
  ShieldCheck,
  Lock,
  Upload,
  FileText,
  Loader2,
} from 'lucide-react';
import { Button } from './ui';
import {
  guestUpload,
  guestStreamChat,
  guestRelease,
} from '../lib/api';

const ACCEPT = '.pdf,.txt,.md,.docx,.html,.htm,.xlsx';
const GUEST_QUESTION_CAP = 2;

export default function GuestChat({ open, onClose, question }) {
  const [phase, setPhase] = useState('upload');
  const [files, setFiles] = useState([]);
  const [session, setSession] = useState(null);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [asked, setAsked] = useState(0);

  const bottom = useRef(null);
  const fileInput = useRef(null);
  const seeded = useRef(false);
  const abortRef = useRef(null);
  const busyRef = useRef(false);
  const openRef = useRef(open);

  openRef.current = open;

  useEffect(() => {
    if (open) return;

    abortRef.current?.abort();
    abortRef.current = null;

    if (session?.collection_id) {
      guestRelease(session.collection_id);
    }

    busyRef.current = false;
    setBusy(false);
    setPhase('upload');
    setFiles([]);
    setSession(null);
    setMessages([]);
    setInput('');
    setError('');
    setAsked(0);
    seeded.current = false;
  }, [open]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  useEffect(() => {
    if (!open || !question || seeded.current) return;

    seeded.current = true;
    setInput(question);
  }, [open, question]);

  function patchLast(fn) {
    setMessages((current) => {
      if (!current.length) return current;

      const next = [...current];
      const lastIndex = next.length - 1;
      next[lastIndex] = fn(next[lastIndex]);

      return next;
    });
  }

  async function upload() {
    if (busyRef.current) return;

    if (!files.length) {
      setError('Choose a document first.');
      return;
    }

    busyRef.current = true;
    setBusy(true);
    setError('');

    try {
      const data = await guestUpload(files);

      if (!openRef.current) {
        if (data?.collection_id) {
          guestRelease(data.collection_id);
        }
        return;
      }

      setSession(data);
      setPhase('chat');
    } catch (err) {
      if (openRef.current) {
        setError(err.message || 'The document could not be uploaded.');
      }
    } finally {
      busyRef.current = false;
      if (openRef.current) setBusy(false);
    }
  }

  async function ask(event) {
    event?.preventDefault();

    const text = input.trim();

    if (
      !text ||
      busyRef.current ||
      !session ||
      asked >= GUEST_QUESTION_CAP
    ) {
      return;
    }

    busyRef.current = true;
    setInput('');
    setError('');
    setBusy(true);
    setAsked((count) => count + 1);

    setMessages((current) => [
      ...current,
      { role: 'user', content: text },
      {
        role: 'assistant',
        content: '',
        sources: [],
        trace: [],
        pending: true,
        warning: '',
      },
    ]);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await guestStreamChat(
        session.collection_id,
        text,
        (event) => {
          if (!openRef.current) return;

          if (event.type === 'trace') {
            patchLast((message) => ({
              ...message,
              trace: event.trace || [],
            }));
          } else if (event.type === 'reset') {
            patchLast((message) => ({
              ...message,
              content: '',
            }));
          } else if (event.type === 'token') {
            patchLast((message) => ({
              ...message,
              content: message.content + (event.text || ''),
            }));
          } else if (event.type === 'warning') {
            patchLast((message) => ({
              ...message,
              warning: event.message || 'Part of the response could not be read.',
            }));
          } else if (event.type === 'final') {
            patchLast((message) => ({
              ...message,
              content: event.answer || '',
              sources: event.sources || [],
              trace: event.trace || message.trace || [],
              pending: false,
              warning: '',
            }));
          }
        },
        controller.signal
      );
    } catch (err) {
      if (err.name !== 'AbortError' && openRef.current) {
        setError(err.message || 'The answer could not be completed.');

        patchLast((message) => ({
          ...message,
          pending: false,
          warning: message.content
            ? 'This answer may be incomplete because the connection was interrupted.'
            : 'The answer could not be generated. Please try again.',
        }));
      }
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
      }

      busyRef.current = false;

      if (openRef.current) {
        setBusy(false);
      }
    }
  }

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[120] grid place-items-center p-4">
      <div
        className="absolute inset-0 bg-black/70 backdrop-blur-md"
        onClick={onClose}
      />

      <div className="glass-strong relative flex h-[80vh] w-full max-w-3xl flex-col overflow-hidden rounded-3xl animate-fade-up">
        <header className="flex items-center gap-3 border-b border-border px-5 py-4">
          <div className="grid h-8 w-8 place-items-center rounded-xl bg-accent/15 text-accent">
            <Sparkles size={16} />
          </div>

          <div className="flex-1">
            <div className="font-display text-xl leading-none">
              Try Docs Assistant
            </div>
            <div className="mt-1 flex items-center gap-1.5 text-xs text-muted">
              <ShieldCheck size={12} />
              Guest mode — your file stays private and is deleted automatically
            </div>
          </div>

          <button
            onClick={onClose}
            aria-label="Close guest chat"
            className="rounded-full p-2 text-muted hover:bg-surface-2 hover:text-ink"
          >
            <X size={16} />
          </button>
        </header>

        {phase === 'upload' && (
          <div className="flex-1 overflow-y-auto px-6 py-6">
            <div className="mx-auto max-w-md space-y-5 pt-4 text-center">
              <div className="mx-auto grid h-14 w-14 place-items-center rounded-2xl glass text-accent">
                <Upload size={22} strokeWidth={1.75} />
              </div>

              <h2 className="font-display text-3xl leading-tight">
                Upload a document to try it
              </h2>

              <p className="text-sm text-muted">
                PDF, Word, Excel, HTML, Markdown, or text. We'll answer questions
                about it — nothing is saved, and it's deleted when you close this window.
              </p>

              <div
                onClick={() => fileInput.current?.click()}
                onDragOver={(event) => event.preventDefault()}
                onDrop={(event) => {
                  event.preventDefault();
                  setFiles(Array.from(event.dataTransfer.files));
                  setError('');
                }}
                className="glass mt-2 cursor-pointer rounded-2xl border border-dashed border-border px-6 py-8 transition hover:border-accent"
              >
                <input
                  ref={fileInput}
                  type="file"
                  accept={ACCEPT}
                  multiple
                  className="hidden"
                  onChange={(event) => {
                    setFiles(Array.from(event.target.files || []));
                    setError('');
                  }}
                />

                {files.length === 0 ? (
                  <p className="text-sm text-muted">
                    Click to choose, or drag a file here
                  </p>
                ) : (
                  <div className="flex flex-col items-center gap-2">
                    {files.map((file, index) => (
                      <div
                        key={`${file.name}-${index}`}
                        className="flex items-center gap-2 text-sm text-ink"
                      >
                        <FileText size={14} className="text-accent" />
                        <span className="max-w-[280px] truncate">
                          {file.name}
                        </span>
                        <span className="text-xs text-muted">
                          ({Math.round(file.size / 1024)} KB)
                        </span>
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
                {busy ? (
                  <>
                    <Loader2 size={15} className="mr-2 animate-spin" />
                    Indexing…
                  </>
                ) : (
                  'Start asking'
                )}
              </Button>

              {error && (
                <p role="alert" className="text-sm text-danger">
                  {error}
                </p>
              )}
            </div>
          </div>
        )}

        {phase === 'chat' && (
          <>
            <div className="flex-1 space-y-5 overflow-y-auto px-6 py-6">
              <div className="flex items-center gap-2 rounded-xl border border-border bg-surface/60 px-3 py-2 text-xs text-muted">
                <FileText size={12} className="text-accent" />
                <span className="truncate">{files[0]?.name}</span>
                <span>· {session?.chunks} chunks indexed</span>
              </div>

              {messages.map((message, index) => (
                <div key={index}>
                  {message.role === 'user' ? (
                    <div className="ml-auto max-w-[78%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-surface-2 px-4 py-3 text-sm">
                      {message.content}
                    </div>
                  ) : (
                    <div className="max-w-[85%] space-y-3">
                      <div className="whitespace-pre-wrap text-sm leading-relaxed text-ink">
                        {message.content ||
                          (message.pending ? (
                            <span className="animate-pulse-soft text-muted">
                              Thinking…
                            </span>
                          ) : '')}
                      </div>

                      {message.warning && (
                        <p className="text-xs text-danger">
                          {message.warning}
                        </p>
                      )}

                      {message.sources?.length > 0 && (
                        <div className="space-y-2">
                          {message.sources.map((source, sourceIndex) => (
                            <div
                              key={sourceIndex}
                              className="glass rounded-xl px-4 py-3 text-sm"
                            >
                              <div className="text-xs text-muted">
                                {source.source}
                                {source.location
                                  ? ` · ${source.location.split(', ').slice(1).join(', ')}`
                                  : ''}
                              </div>

                              {source.quote && (
                                <details className="mt-1.5">
                                  <summary className="cursor-pointer text-xs text-muted transition hover:text-ink">
                                    Show quote
                                  </summary>
                                  <p className="mt-1.5 border-l-2 border-[color:var(--color-blue)] pl-3 text-sm italic text-ink/90">
                                    “{source.quote}”
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

            {messages.some(
              (message) =>
                message.role === 'assistant' &&
                !message.pending &&
                message.content
            ) &&
              asked < GUEST_QUESTION_CAP && (
                <div className="border-t border-border bg-surface-2/40 px-6 py-3 text-sm">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-2 text-muted">
                      <Lock size={13} />
                      Sign in to keep chats, upload many docs, and search your team's library.
                    </div>

                    <div className="flex gap-2">
                      <Link
                        to="/register"
                        className="rounded-full bg-accent px-3.5 py-1.5 text-xs font-semibold text-canvas transition hover:bg-accent-hover"
                      >
                        Create an organisation
                      </Link>

                      <Link
                        to="/login"
                        className="rounded-full border border-border px-3.5 py-1.5 text-xs text-ink transition hover:bg-surface-2"
                      >
                        Sign in
                      </Link>
                    </div>
                  </div>
                </div>
              )}

            {error && (
              <p role="alert" className="px-6 pb-2 text-sm text-danger">
                {error}
              </p>
            )}

            {asked < GUEST_QUESTION_CAP ? (
              <form
                onSubmit={ask}
                className="flex items-end gap-3 border-t border-border bg-black/20 p-4"
              >
                <textarea
                  rows={2}
                  value={input}
                  disabled={busy}
                  onChange={(event) => setInput(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && !event.shiftKey) {
                      event.preventDefault();
                      ask();
                    }
                  }}
                  placeholder={`Ask anything about the document… (${GUEST_QUESTION_CAP - asked} left)`}
                  className="flex-1 resize-none rounded-2xl border border-border bg-surface-2 px-4 py-3 text-sm placeholder:text-faint focus:border-accent focus:outline-none"
                />

                <Button
                  variant="primary"
                  type="submit"
                  disabled={busy || !input.trim()}
                  size="icon"
                  className="!h-11 !w-11"
                >
                  <Send size={16} strokeWidth={2.25} />
                </Button>
              </form>
            ) : (
              <div className="border-t border-border bg-surface-2/60 p-5">
                <div className="mx-auto max-w-md text-center">
                  <div className="mx-auto mb-3 grid h-11 w-11 place-items-center rounded-2xl glass text-accent">
                    <Lock size={18} />
                  </div>

                  <h3 className="font-display text-2xl leading-tight">
                    You've used your free questions
                  </h3>

                  <p className="mt-2 text-sm text-muted">
                    Sign in to keep asking, save your chats, upload many documents,
                    and search your team's library.
                  </p>

                  <div className="mt-5 flex justify-center gap-2">
                    <Link
                      to="/register"
                      className="rounded-full bg-accent px-5 py-2.5 text-sm font-semibold text-canvas transition hover:bg-accent-hover"
                    >
                      Create an organisation
                    </Link>

                    <Link
                      to="/login"
                      className="rounded-full border border-border px-5 py-2.5 text-sm text-ink transition hover:bg-surface-2"
                    >
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
