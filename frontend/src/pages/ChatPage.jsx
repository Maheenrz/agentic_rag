
import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import MessageBubble from '../components/MessageBubble';
import { Button, Textarea } from '../components/ui';
import { useApp } from '../context/AppContext';
import { api, streamChat } from '../lib/api';
import { Send } from 'lucide-react';

export default function ChatPage() {
  const { threadId } = useParams();
  const navigate = useNavigate();
  const { threads, selected, refreshThreads } = useApp();

  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [loadingMessages, setLoadingMessages] = useState(false);

  const streaming = useRef(false);
  const currentId = useRef(threadId);
  const messageCount = useRef(0);
  const bottom = useRef(null);
  const busyRef = useRef(false);

  currentId.current = threadId;

  useEffect(() => {
    let active = true;

    async function loadMessages() {
      if (streaming.current) return;

      if (!threadId) {
        setMessages([]);
        setError('');
        setLoadingMessages(false);
        return;
      }

      setLoadingMessages(true);
      setError('');

      try {
        const result = await api(
          `/threads/${encodeURIComponent(threadId)}/messages`
        );

        if (active) {
          setMessages(Array.isArray(result) ? result : []);
        }
      } catch (err) {
        if (active) setError(err.message);
      } finally {
        if (active) setLoadingMessages(false);
      }
    }

    loadMessages();

    return () => {
      active = false;
    };
  }, [threadId]);

  useEffect(() => {
    messageCount.current = messages.length;
  }, [messages]);

  useEffect(() => {
    const leaving = threadId;

    return () => {
      if (leaving && messageCount.current) {
        api(
          `/threads/${encodeURIComponent(leaving)}/finalize`,
          { method: 'POST' }
        ).catch(() => {});
      }
    };
  }, [threadId]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' });
  }, [messages]);

  function patchLast(fn) {
    setMessages((current) => {
      if (!current.length) return current;

      const next = [...current];
      const lastIndex = next.length - 1;

      next[lastIndex] = fn(next[lastIndex]);

      return next;
    });
  }

  async function send() {
    const question = input.trim();

    if (!question || busyRef.current) return;

    busyRef.current = true;
    setInput('');
    setError('');
    setBusy(true);
    streaming.current = true;

    const isNewChat = !threadId;
    let id = threadId;

    try {
      if (!id) {
        const created = await api('/threads', {
          method: 'POST',
        });

        id = created.thread_id;

        if (!id) {
          throw new Error('The server did not create a conversation.');
        }

        navigate(`/chat/${id}`);
      }

      setMessages((current) => [
        ...current,
        { role: 'user', content: question },
        {
          role: 'assistant',
          content: '',
          trace: [],
          sources: [],
          pending: true,
          warning: '',
        },
      ]);

      await streamChat(
        id,
        {
          question,
          collection_ids: selected.length ? selected : null,
        },
        (event) => {
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
        }
      );

      if (isNewChat) {
        await api(`/threads/${encodeURIComponent(id)}`, {
          method: 'PATCH',
          body: { title: question.slice(0, 60) },
        });
      }
    } catch (err) {
      setError(err.message || 'The response could not be completed.');

      patchLast((message) => ({
        ...message,
        pending: false,
        warning: message.content
          ? 'This answer may be incomplete because the connection was interrupted.'
          : 'The answer could not be generated. Please try again.',
      }));
    } finally {
      streaming.current = false;
      busyRef.current = false;
      setBusy(false);

      refreshThreads().catch(() => {});

      if (id && currentId.current === id) {
        api(`/threads/${encodeURIComponent(id)}/messages`)
          .then((result) => {
            if (currentId.current === id) {
              setMessages(Array.isArray(result) ? result : []);
            }
          })
          .catch(() => {});
      }
    }
  }

  async function rate(message, rating, reason = '', comment = '') {
    if (!threadId || !message?.id) return;

    const url =
      `/threads/${encodeURIComponent(threadId)}/messages/${encodeURIComponent(message.id)}/feedback`;

    try {
      if (rating === 'clear') {
        await api(url, { method: 'DELETE' });
      } else {
        await api(url, {
          method: 'PUT',
          body: { rating, reason, comment },
        });
      }

      const result = await api(
        `/threads/${encodeURIComponent(threadId)}/messages`
      );

      setMessages(Array.isArray(result) ? result : []);
    } catch (err) {
      setError(err.message);
    }
  }

  const current = threads.find((thread) => thread.thread_id === threadId);

  const scope = selected.length
    ? `Searching ${selected.length} selected collection${selected.length > 1 ? 's' : ''}`
    : 'Searching every collection you can access';

  return (
    <>
      <header className="glass sticky top-0 z-10 flex items-center justify-between border-b border-white/5 px-6 py-4">
        <div className="min-w-0">
          <div className="truncate font-display text-2xl leading-tight">
            {current?.title || 'New chat'}
          </div>
          <div className="truncate text-xs text-muted">{scope}</div>
        </div>
      </header>

      <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-6 py-8">
        {loadingMessages && (
          <p className="m-auto text-sm text-muted">Loading conversation…</p>
        )}

        {!loadingMessages && messages.length === 0 && (
          <div className="m-auto max-w-md text-center animate-fade-up">
            <div className="mx-auto mb-5 grid h-12 w-12 place-items-center rounded-2xl glass text-accent">
              <Send size={20} strokeWidth={1.75} />
            </div>

            <h2 className="font-display text-4xl leading-tight">
              Ask your documents anything
            </h2>

            <p className="mt-3 text-sm text-muted">
              Answers come only from the collections you can read. Each answer
              shows its source and the exact sentence it used.
            </p>

            <div className="mt-6 flex flex-wrap justify-center gap-2">
              {[
                'Summarise the latest policy',
                'What is our leave policy?',
                'Compare Q3 and Q4 reports',
              ].map((suggestion) => (
                <button
                  key={suggestion}
                  onClick={() => setInput(suggestion)}
                  className="glass rounded-full px-3.5 py-1.5 text-xs text-muted transition hover:text-ink"
                >
                  {suggestion}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((message, index) => (
          <div key={message.id ?? `pending-${index}`}>
            <MessageBubble
              message={message}
              onRate={rate}
            />

            {message.warning && (
              <p className="mt-2 text-xs text-danger">
                {message.warning}
              </p>
            )}
          </div>
        ))}

        <div ref={bottom} />
      </div>

      {error && (
        <p role="alert" className="px-6 pb-2 text-sm text-danger">
          {error}
        </p>
      )}

      <form
        className="glass sticky bottom-0 flex items-end gap-3 border-t border-white/5 bg-black/20 px-6 py-4"
        onSubmit={(event) => {
          event.preventDefault();
          send();
        }}
      >
        <Textarea
          rows={2}
          className="flex-1 resize-none !rounded-2xl !border-white/10 !bg-white/5"
          placeholder="Ask about your documents..."
          value={input}
          disabled={busy}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault();
              send();
            }
          }}
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
    </>
  );
}
