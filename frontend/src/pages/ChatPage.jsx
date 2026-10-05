import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import MessageBubble from '../components/MessageBubble';
import { Button, Textarea } from '../components/ui';
import { useApp } from '../context/AppContext';
import { api, streamChat } from '../lib/api';
import { Send } from 'lucide-react'

export default function ChatPage() {
  const { threadId } = useParams();
  const navigate = useNavigate();
  const { threads, selected, refreshThreads } = useApp();

  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const streaming = useRef(false);   // true while a reply is streaming in
  const currentId = useRef(threadId);
  const messageCount = useRef(0);
  const bottom = useRef(null);
  currentId.current = threadId;

  // Open a saved chat. Skipped while a reply is streaming into a brand-new chat,
  // because the server copy does not have the streamed messages yet.
  useEffect(() => {
    if (streaming.current) return;
    if (!threadId) {
      setMessages([]);
      return;
    }
    api(`/threads/${threadId}/messages`).then(setMessages).catch((e) => setError(e.message));
  }, [threadId]);

  // When the user leaves a chat, finalize it so later chats can recall it.
  useEffect(() => {
    messageCount.current = messages.length;
  }, [messages]);

  useEffect(() => {
    const leaving = threadId;
    return () => {
      if (leaving && messageCount.current) {
        api(`/threads/${leaving}/finalize`, { method: 'POST' }).catch(() => {});
      }
    };
  }, [threadId]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' });
  }, [messages]);

  // Edits the last (pending) assistant message.
  const patchLast = (fn) =>
    setMessages((ms) => {
      const next = [...ms];
      next[next.length - 1] = fn(next[next.length - 1]);
      return next;
    });

  async function send() {
    const question = input.trim();
    if (!question || busy) return;

    setInput('');
    setError('');
    setBusy(true);
    streaming.current = true;

    const isNewChat = !threadId;
    let id = threadId;

    try {
      if (!id) {
        id = (await api('/threads', { method: 'POST' })).thread_id;
        navigate(`/chat/${id}`);
      }

      setMessages((ms) => [
        ...ms,
        { role: 'user', content: question },
        { role: 'assistant', content: '', trace: [], sources: [], pending: true },
      ]);

      await streamChat(
        id,
        { question, collection_ids: selected.length ? selected : null },
        (ev) => {
          if (ev.type === 'trace') patchLast((m) => ({ ...m, trace: ev.trace }));
          else if (ev.type === 'reset') patchLast((m) => ({ ...m, content: '' }));
          else if (ev.type === 'token') patchLast((m) => ({ ...m, content: m.content + ev.text }));
          else if (ev.type === 'final') {
            // The output guard can rewrite text that was already streamed, so the final answer wins.
            patchLast((m) => ({
              ...m, content: ev.answer, sources: ev.sources, trace: ev.trace, pending: false,
            }));
          } else if (ev.type === 'error') throw new Error(ev.detail);
        }
      );

      if (isNewChat) {
        await api(`/threads/${id}`, { method: 'PATCH', body: { title: question.slice(0, 60) } });
      }
    } catch (e) {
      setError(e.message);
    } finally {
      streaming.current = false;
      setBusy(false);
      refreshThreads().catch(() => {});
      // Reload saved messages so they have ids (needed for feedback).
      // Skip if the user has already moved to a different chat.
      if (id && currentId.current === id) {
        api(`/threads/${id}/messages`).then(setMessages).catch(() => {});
      }
    }
  }

  async function rate(message, rating, reason = '', comment = '') {
    const url = `/threads/${threadId}/messages/${message.id}/feedback`;
    try {
      if (rating === 'clear') await api(url, { method: 'DELETE' });
      else await api(url, { method: 'PUT', body: { rating, reason, comment } });
      setMessages(await api(`/threads/${threadId}/messages`));
    } catch (e) {
      setError(e.message);
    }
  }

  const current = threads.find((t) => t.thread_id === threadId);
  const scope = selected.length
    ? `Searching ${selected.length} selected collection${selected.length > 1 ? 's' : ''}`
    : 'Searching every collection you can access';

  return (
    <>
      <header className="border-b border-border px-6 py-4">
        <div className="font-semibold">{current?.title || 'New chat'}</div>
        <div className="text-xs text-muted">{scope}</div>
      </header>

      <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-6 py-6">
        {messages.length === 0 && (
          <div className="m-auto max-w-md text-center">
            <h2 className="mb-2 text-xl font-semibold">Ask your documents anything</h2>
            <p className="text-sm text-muted">
              Answers come only from the collections you can read. Each answer shows its source and the exact sentence it used.
            </p>
          </div>
        )}

        {messages.map((m, i) => (
          <MessageBubble key={m.id ?? `pending-${i}`} message={m} onRate={rate} />
        ))}
        <div ref={bottom} />
      </div>

      {error && <p className="px-6 text-sm text-danger">{error}</p>}

      <form
        className="flex gap-3 border-t border-border bg-surface px-6 py-4"
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <Textarea
          rows={2}
          className="flex-1 resize-none"
          placeholder="Ask about your documents… (Enter to send, Shift+Enter for a new line)"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        <Button variant="primary" type="submit" disabled={busy || !input.trim()} className="px-5">
          <Send size={16} strokeWidth={2.25} color="white" />
        </Button>
      </form>
    </>
  );
}