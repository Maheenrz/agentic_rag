import { useState } from 'react';
import { api } from '../../lib/api';
import { guard, useAsync } from '../../lib/hooks';
import { Button, Card, Check, Input, Label, Loading, Pill, Select, Table } from '../../components/ui';

const EMPTY = { name: '', description: '', visibility: 'restricted' };

export default function CollectionsPage() {
  const cols = useAsync(() => api('/collections'));
  const [form, setForm] = useState(EMPTY);
  const [openId, setOpenId] = useState(null);

  if (!cols.data) return <Loading error={cols.error} />;

  const shared = cols.data.filter((c) => !c.owner_user_id);
  const open = shared.find((c) => c.collection_id === openId);
  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value });

  async function create() {
    await api('/collections', {
      method: 'POST',
      body: { ...form, name: form.name.trim(), description: form.description.trim() },
    });
    setForm(EMPTY);
    cols.reload();
  }

  return (
    <div className="space-y-6">
      <Card>
        <h2 className="mb-3 text-lg font-semibold">Create collection</h2>
        <div className="flex flex-wrap items-end gap-3">
          <Label className="min-w-[160px] flex-1">
            Name
            <Input className="mt-1" value={form.name} onChange={set('name')} />
          </Label>
          <Label className="min-w-[160px] flex-1">
            Description
            <Input className="mt-1" value={form.description} onChange={set('description')} />
          </Label>
          <Label>
            Visibility
            <Select className="mt-1" value={form.visibility} onChange={set('visibility')}>
              <option value="restricted">Restricted (granted people only)</option>
              <option value="org">Org-wide (everyone)</option>
            </Select>
          </Label>
          <Button variant="primary" onClick={guard(create)}>Create</Button>
        </div>
      </Card>

      <Card>
        <Table
          head={['Name', 'Visibility', '']}
          rows={shared.map((c) => [
            c.name,
            <Pill>{c.visibility}</Pill>,
            <Button size="sm" onClick={() => setOpenId(c.collection_id)}>Manage</Button>,
          ])}
        />
        {shared.length === 0 && <p className="mt-2 text-sm text-muted">No shared collections yet.</p>}
      </Card>

      {open && <CollectionManager key={open.collection_id} collection={open} />}
    </div>
  );
}

function CollectionManager({ collection }) {
  const id = collection.collection_id;

  const data = useAsync(async () => {
    const [docs, acl, users, groups] = await Promise.all([
      api(`/collections/${id}/documents`),
      api(`/collections/${id}/acl`),
      api('/org/users'),
      api('/org/groups'),
    ]);
    return { docs, acl, users, groups };
  }, [id]);

  if (!data.data) return <Card><Loading error={data.error} /></Card>;

  const { docs, acl, users, groups } = data.data;

  async function deleteDoc(d) {
    if (!confirm(`Delete ${d.filename}? Chats built on it will stop matching.`)) return;
    await api(`/collections/${id}/documents/${d.doc_id}`, { method: 'DELETE' });
    data.reload();
  }

  return (
    <Card className="space-y-6">
      <h2 className="text-lg font-semibold">Manage: {collection.name}</h2>

      <div>
        <h3 className="mb-2 font-medium">Documents</h3>
        {docs.length === 0 ? (
          <p className="text-sm text-muted">No documents yet. Upload from the Upload page.</p>
        ) : (
          <Table
            head={['File', 'Version', 'Status', 'Chunks', 'Findings', '']}
            rows={docs.map((d) => [
              d.filename,
              `v${d.version}`,
              d.status,
              d.chunk_count,
              d.findings.length,
              <Button size="sm" variant="danger" onClick={guard(() => deleteDoc(d))}>Delete</Button>,
            ])}
          />
        )}
      </div>

      {/* Keyed by the saved ACL, so the form resets after each save. */}
      <AccessForm
        key={JSON.stringify(acl)}
        collectionId={id}
        acl={acl}
        users={users}
        groups={groups}
        onSaved={data.reload}
      />
    </Card>
  );
}

function AccessForm({ collectionId, acl, users, groups, onSaved }) {
  const [userIds, setUserIds] = useState(acl.user_ids);
  const [groupIds, setGroupIds] = useState(acl.group_ids);
  const [visibility, setVisibility] = useState(acl.visibility);

  const toggle = (list, setList, value) =>
    setList((l) => (l.includes(value) ? l.filter((x) => x !== value) : [...l, value]));

  async function save() {
    await api(`/collections/${collectionId}/acl`, {
      method: 'PUT',
      body: { user_ids: userIds, group_ids: groupIds, visibility },
    });
    onSaved();
  }

  return (
    <div className="space-y-4">
      <h3 className="font-medium">Who can read it</h3>

      <Label>
        Visibility
        <Select className="mt-1 max-w-xs" value={visibility} onChange={(e) => setVisibility(e.target.value)}>
          <option value="restricted">Restricted</option>
          <option value="org">Org-wide (everyone)</option>
        </Select>
      </Label>

      <div>
        <p className="mb-1 text-xs text-muted">Users</p>
        <div className="grid gap-1 sm:grid-cols-3">
          {users.map((u) => (
            <Check
              key={u.user_id}
              label={u.username}
              checked={userIds.includes(u.user_id)}
              onChange={() => toggle(userIds, setUserIds, u.user_id)}
            />
          ))}
        </div>
      </div>

      <div>
        <p className="mb-1 text-xs text-muted">Groups</p>
        <div className="grid gap-1 sm:grid-cols-3">
          {groups.map((g) => (
            <Check
              key={g.group_id}
              label={g.name}
              checked={groupIds.includes(g.group_id)}
              onChange={() => toggle(groupIds, setGroupIds, g.group_id)}
            />
          ))}
        </div>
      </div>

      <Button variant="primary" onClick={guard(save)}>Save access</Button>
    </div>
  );
}