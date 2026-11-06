import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { REASONS } from '../lib/constants';
import { Button, Input, Select } from './ui';

function normalizeMarkdown(content) {
  return String(content || '')
    .replace(/\\\*/g, '*')
    .replace(/\\_/g, '_')
    .replace(/\\#/g, '#')
    .replace(/\\`/g, '`')
    .replace(/\r\n?/g, '\n');
}

export default function MessageBubble({ message: m, onRate }) {
  const [showForm, setShowForm] = useState(false);
  const [reason, setReason] = useState('wrong_answer');
  const [comment, setComment] = useState('');

  if (m.role === 'user') {
    return (
      <div className="ml-auto max-w-[78%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-surface-2 px-4 py-3">
        {m.content}
      </div>
    );
  }

  const up = m.feedback?.rating === 1;
  const down = m.feedback?.rating === -1;

  return (
    <div className="max-w-[760px] min-w-0 space-y-3">
      <div className="break-words leading-relaxed">
        {m.content ? (
          <div className="nexmind-markdown">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                h1: ({ children }) => (
                  <h1 className="mb-3 mt-5 text-2xl font-semibold">{children}</h1>
                ),
                h2: ({ children }) => (
                  <h2 className="mb-2 mt-5 text-xl font-semibold">{children}</h2>
                ),
                h3: ({ children }) => (
                  <h3 className="mb-2 mt-4 text-lg font-semibold">{children}</h3>
                ),
                p: ({ children }) => (
                  <p className="mb-3 last:mb-0">{children}</p>
                ),
                ul: ({ children }) => (
                  <ul className="mb-3 list-disc space-y-1 pl-6">{children}</ul>
                ),
                ol: ({ children }) => (
                  <ol className="mb-3 list-decimal space-y-1 pl-6">{children}</ol>
                ),
                li: ({ children }) => <li className="pl-1">{children}</li>,
                strong: ({ children }) => (
                  <strong className="font-semibold text-ink">{children}</strong>
                ),
                blockquote: ({ children }) => (
                  <blockquote className="my-3 border-l-2 border-accent pl-4 text-muted">
                    {children}
                  </blockquote>
                ),
                code: ({ className, children, ...props }) => {
                  const block = Boolean(className);
                  return block ? (
                    <code
                      className="block overflow-x-auto rounded-lg bg-surface-2 p-3 text-sm"
                      {...props}
                    >
                      {children}
                    </code>
                  ) : (
                    <code
                      className="rounded bg-surface-2 px-1 py-0.5 text-sm"
                      {...props}
                    >
                      {children}
                    </code>
                  );
                },
                pre: ({ children }) => (
                  <pre className="my-3 overflow-x-auto rounded-lg border border-border">
                    {children}
                  </pre>
                ),
                a: ({ children, href }) => (
                  <a
                    href={href}
                    target="_blank"
                    rel="noreferrer"
                    className="text-accent underline underline-offset-2"
                  >
                    {children}
                  </a>
                ),
                table: ({ children }) => (
                  <div className="my-3 overflow-x-auto">
                    <table className="w-full border-collapse text-sm">{children}</table>
                  </div>
                ),
                th: ({ children }) => (
                  <th className="border border-border bg-surface-2 px-3 py-2 text-left">
                    {children}
                  </th>
                ),
                td: ({ children }) => (
                  <td className="border border-border px-3 py-2">{children}</td>
                ),
              }}
            >
              {normalizeMarkdown(m.content)}
            </ReactMarkdown>
          </div>
        ) : (
          m.pending ? '…' : ''
        )}
      </div>

      {m.warning && (
        <p className="text-sm text-muted">{m.warning}</p>
      )}

      {m.sources?.length > 0 && (
        <div className="space-y-2">
          {m.sources.map((s, i) => (
            <div
              key={i}
              className="rounded-xl border border-border bg-surface px-4 py-3 text-sm"
            >
              <div className="text-xs text-muted">
                {s.tool} · {s.location || s.source}
              </div>
              {s.quote && (
                <p className="mt-1.5 border-l-2 border-accent pl-3 italic text-ink/90">
                  “{s.quote}”
                </p>
              )}
            </div>
          ))}
        </div>
      )}

      {m.trace?.length > 0 && (
        <details className="text-sm text-muted">
          <summary className="cursor-pointer hover:text-ink">
            Reasoning trace ({m.trace.length} steps)
          </summary>
          <ol className="mt-2 list-decimal space-y-1 pl-5">
            {m.trace.map((t, i) => <li key={i}>{t}</li>)}
          </ol>
        </details>
      )}

      {m.id && (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            className={up ? 'border-accent text-accent' : ''}
            onClick={() => onRate(m, up ? 'clear' : 'up')}
          >
            Helpful
          </Button>
          <Button
            size="sm"
            className={down ? 'border-danger text-danger' : ''}
            onClick={() => down ? onRate(m, 'clear') : setShowForm(v => !v)}
          >
            Not helpful
          </Button>

          {showForm && !down && (
            <div className="w-full space-y-2 pt-1">
              <div className="flex flex-wrap gap-2">
                <Select
                  value={reason}
                  onChange={e => setReason(e.target.value)}
                  className="w-auto"
                >
                  {REASONS.map(([value, label]) => (
                    <option key={value} value={value}>{label}</option>
                  ))}
                </Select>
                <Input
                  className="min-w-[200px] flex-1"
                  placeholder="What was wrong? (optional)"
                  value={comment}
                  onChange={e => setComment(e.target.value)}
                />
                <Button
                  variant="primary"
                  size="sm"
                  onClick={() => {
                    onRate(m, 'down', reason, comment.trim());
                    setShowForm(false);
                    setComment('');
                  }}
                >
                  Send
                </Button>
              </div>
              <p className="text-xs text-muted">
                Admins can see this comment. Don’t include personal or confidential details.
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}