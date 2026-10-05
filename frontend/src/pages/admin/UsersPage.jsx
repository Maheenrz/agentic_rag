import { useState } from 'react';
import { useApp } from '../../context/AppContext';
import { api } from '../../lib/api';
import { guard, useAsync } from '../../lib/hooks';
import { Button, Card, Input, Label, Loading, Pill, Select, Table } from '../../components/ui';

const EMPTY = { username: '', password: '', role: 'member' };

export default function UsersPage() {
  const { me } = useApp();
  const users = useAsync(() => api('/org/users'));
  const [form, setForm] = useState(EMPTY);

  if (!users.data) return <Loading error={users.error} />;

  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value });

  async function create() {
    await api('/org/users', {
      method: 'POST',
      body: { ...form, username: form.username.trim() },
    });
    setForm(EMPTY);
    users.reload();
  }

  async function remove(u) {
    if (!confirm(`Delete ${u.username}? Their chats and private documents will be removed.`)) return;
    await api(`/org/users/${u.user_id}`, { method: 'DELETE' });
    users.reload();
  }

  return (
    <div className="space-y-6">
      <Card>
        <h2 className="mb-3 text-lg font-semibold">Create user</h2>
        <div className="flex flex-wrap items-end gap-3">
          <Label className="min-w-[160px] flex-1">
            Username
            <Input className="mt-1" value={form.username} onChange={set('username')} />
          </Label>
          <Label className="min-w-[160px] flex-1">
            Password
            <Input className="mt-1" type="password" value={form.password} onChange={set('password')} />
          </Label>
          <Label>
            Role
            <Select className="mt-1" value={form.role} onChange={set('role')}>
              <option value="member">Member</option>
              <option value="admin">Admin</option>
            </Select>
          </Label>
          <Button variant="primary" onClick={guard(create)}>Create</Button>
        </div>
      </Card>

      <Card>
        <h2 className="mb-3 text-lg font-semibold">Members</h2>
        <Table
          head={['Username', 'Role', '']}
          rows={users.data.map((u) => [
            u.username,
            <Pill>{u.role}</Pill>,
            u.user_id === me.user_id ? (
              <span className="text-xs text-muted">(you)</span>
            ) : (
              <Button size="sm" variant="danger" onClick={guard(() => remove(u))}>Delete</Button>
            ),
          ])}
        />
      </Card>
    </div>
  );
}