import { useState } from 'react';
import { REASONS } from '../lib/constants';
import { Button, Input, Select } from './ui';

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
    <div className="max-w-[760px] space-y-3">
      <div className="whitespace-pre-wrap break-words leading-relaxed">
        {m.content || (m.pending ? '…' : '')}
      </div>

      {m.sources?.length > 0 && (
        <div className="space-y-2">
          {m.sources.map((s, i) => (
            <div key={i} className="rounded-xl border border-border bg-surface px-4 py-3 text-sm">
              <div className="text-xs text-muted">{s.tool} · {s.location || s.source}</div>
              {s.quote && (
                <p className="mt-1.5 border-l-2 border-accent pl-3 italic text-ink/90">“{s.quote}”</p>
              )}
            </div>
          ))}
        </div>
      )}

      {m.trace?.length > 0 && (
        <details className="text-sm text-muted">
          <summary className="cursor-pointer hover:text-ink">Reasoning trace ({m.trace.length} steps)</summary>
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
            onClick={() => (down ? onRate(m, 'clear') : setShowForm((v) => !v))}
          >
            Not helpful
          </Button>

          {showForm && !down && (
            <div className="w-full space-y-2 pt-1">
              <div className="flex flex-wrap gap-2">
                <Select value={reason} onChange={(e) => setReason(e.target.value)} className="w-auto">
                  {REASONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </Select>
                <Input
                  className="min-w-[200px] flex-1"
                  placeholder="What was wrong? (optional)"
                  value={comment}
                  onChange={(e) => setComment(e.target.value)}
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