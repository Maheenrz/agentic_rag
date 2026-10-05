import { api } from '../../lib/api';
import { useAsync } from '../../lib/hooks';
import { Card, Loading, Table } from '../../components/ui';

export default function AuditPage() {
  const { data, error } = useAsync(() => api('/org/audit?limit=100'));

  if (!data) return <Loading error={error} />;

  return (
    <Card>
      <Table
        head={['When', 'User', 'Action', 'Detail']}
        rows={data.map((r) => [
          new Date(r.ts * 1000).toLocaleString(),
          r.username || '—',
          r.action,
          <pre key="d" className="max-w-md whitespace-pre-wrap break-all text-xs text-muted">
            {JSON.stringify(r.detail).slice(0, 200)}
          </pre>,
        ])}
      />
    </Card>
  );
}