import { api } from '../../lib/api';
import { useAsync } from '../../lib/hooks';
import { Card, Loading, Pill, Stat } from '../../components/ui';

export default function FeedbackPage() {
  const { data, error } = useAsync(() =>
    Promise.all([
      api('/org/feedback/summary'),
      api('/org/feedback?rating=down&limit=50'),
    ])
  );

  if (!data) return <Loading error={error} />;

  const [summary, rows] = data;
  const reasons =
    Object.entries(summary.down_reasons).map(([k, v]) => `${k}: ${v}`).join(' · ') || 'none';

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat value={summary.total} label="ratings" />
        <Stat value={summary.up} label="helpful" />
        <Stat value={summary.down} label="not helpful" />
        <Stat
          value={summary.satisfaction_pct == null ? '—' : `${summary.satisfaction_pct}%`}
          label="satisfaction"
        />
      </div>

      <p className="text-sm text-muted">Thumbs-down reasons: {reasons}</p>

      <h2 className="text-lg font-semibold">Recent thumbs-down answers</h2>
      {rows.length === 0 && <p className="text-muted">No thumbs-down yet.</p>}

      {rows.map((r) => (
        <Card key={r.id} className="space-y-3">
          <div className="flex flex-wrap items-center gap-3 text-xs text-muted">
            <span>{r.username}</span>
            <span>{new Date(r.created_at * 1000).toLocaleString()}</span>
            <Pill>{r.reason || 'no reason'}</Pill>
          </div>
          <p><span className="font-semibold">Q: </span>{r.question}</p>
          <div className="whitespace-pre-wrap rounded-lg bg-surface-2 p-3 text-sm">{r.answer}</div>
          {r.trace?.length > 0 && (
            <details className="text-xs text-muted">
              <summary className="cursor-pointer hover:text-ink">Reasoning trace</summary>
              <ol className="mt-1 list-decimal pl-5">
                {r.trace.map((t, i) => <li key={i}>{t}</li>)}
              </ol>
            </details>
          )}
          {r.comment && <p className="text-sm">Comment: {r.comment}</p>}
          {r.sources.length > 0 && (
            <p className="text-xs text-muted">
              Sources: {r.sources.map((s) => s.location || s.source).join('; ')}
            </p>
          )}
        </Card>
      ))}
    </div>
  );
}