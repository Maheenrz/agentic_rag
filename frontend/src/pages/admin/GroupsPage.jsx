import { useState } from 'react';
import { api } from '../../lib/api';
import { guard, useAsync } from '../../lib/hooks';
import { Button, Card, Input, Loading, Select } from '../../components/ui';

export default function GroupsPage() {
  const groups = useAsync(() => api('/org/groups'));
  const users = useAsync(() => api('/org/users'));
  const [name, setName] = useState('');

  if (!groups.data || !users.data) return <Loading error={groups.error || users.error} />;

  const names = Object.fromEntries(users.data.map((u) => [u.user_id, u.username]));

  async function create() {
    await api('/org/groups', { method: 'POST', body: { name: name.trim() } });
    setName('');
    groups.reload();
  }

  async function removeGroup(g) {
    if (!confirm(`Delete group "${g.name}"? Collections it grants will lose that access.`)) return;
    await api(`/org/groups/${g.group_id}`, { method: 'DELETE' });
    groups.reload();
  }

  async function addMember(g, userId) {
    await api(`/org/groups/${g.group_id}/members`, { method: 'POST', body: { user_id: userId } });
    groups.reload();
  }

  async function removeMember(g, userId) {
    await api(`/org/groups/${g.group_id}/members/${userId}`, { method: 'DELETE' });
    groups.reload();
  }

  return (
    <div className="space-y-6">
      <Card>
        <h2 className="mb-3 text-lg font-semibold">Create group</h2>
        <div className="flex gap-3">
          <Input placeholder="New group name" value={name} onChange={(e) => setName(e.target.value)} />
          <Button variant="primary" onClick={guard(create)}>Create</Button>
        </div>
      </Card>

      {groups.data.length === 0 && <p className="text-muted">No groups yet.</p>}

      {groups.data.map((g) => {
        const addable = users.data.filter((u) => !g.member_ids.includes(u.user_id));
        return (
          <Card key={g.group_id} className="space-y-3">
            <div className="flex items-center justify-between">
              <h3 className="font-semibold">{g.name}</h3>
              <Button size="sm" variant="danger" onClick={guard(() => removeGroup(g))}>Delete group</Button>
            </div>

            <div className="flex flex-wrap gap-2">
              {g.member_ids.length === 0 && <span className="text-sm text-muted">No members yet.</span>}
              {g.member_ids.map((uid) => (
                <span
                  key={uid}
                  className="flex items-center gap-2 rounded-full border border-accent bg-surface-2 px-3 py-1 text-sm"
                >
                  {names[uid] || '(removed)'}
                  <button
                    className="text-muted hover:text-ink"
                    onClick={guard(() => removeMember(g, uid))}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>

            <Select
              className="max-w-xs"
              value=""
              onChange={(e) => {
                const uid = e.target.value;
                if (uid) guard(() => addMember(g, uid))();
              }}
            >
              <option value="">Add member…</option>
              {addable.map((u) => <option key={u.user_id} value={u.user_id}>{u.username}</option>)}
            </Select>
          </Card>
        );
      })}
    </div>
  );
}