import { useState } from 'react';
import { api } from '../../lib/api';
import { guard, useAsync } from '../../lib/hooks';
import {
  Button,
  Card,
  Check,
  Input,
  Label,
  Loading,
  Pill,
  Select,
  Table,
} from '../../components/ui';

const EMPTY = {
  name: '',
  description: '',
  visibility: 'restricted',
};

const NAME_MAX = 64;
const DESC_MAX = 240;

function getApiErrorMessage(err, fallback) {
  const message =
    typeof err?.message === 'string' ? err.message : '';

  if (
    err?.status === 409 ||
    /already exists|duplicate key|unique constraint/i.test(message)
  ) {
    return 'A collection with that name already exists.';
  }

  return message || fallback;
}

export default function CollectionsPage() {
  const cols = useAsync(() => api('/collections'));
  const [form, setForm] = useState(EMPTY);
  const [formError, setFormError] = useState('');
  const [openId, setOpenId] = useState(null);

  if (!cols.data) {
    return <Loading error={cols.error} />;
  }

  const shared = cols.data.filter((collection) => !collection.owner_user_id);
  const open = shared.find((collection) => collection.collection_id === openId);

  const set = (key) => (event) => {
    setForm((current) => ({
      ...current,
      [key]: event.target.value,
    }));
  };

  async function create(event) {
    event.preventDefault();
    setFormError('');

    const name = form.name.trim();
    const description = form.description.trim();

    if (!name) {
      setFormError('Collection name is required.');
      return;
    }

    if (name.length > NAME_MAX) {
      setFormError(
        `Collection name must be ${NAME_MAX} characters or fewer.`
      );
      return;
    }

    if (description.length > DESC_MAX) {
      setFormError(
        `Description must be ${DESC_MAX} characters or fewer.`
      );
      return;
    }

    // This check improves feedback. The backend must enforce uniqueness too.
    if (
      cols.data.some(
        (collection) =>
          collection.name.trim().toLowerCase() === name.toLowerCase()
      )
    ) {
      setFormError('A collection with that name already exists.');
      return;
    }

    try {
      await api('/collections', {
        method: 'POST',
        body: {
          name,
          description,
          visibility: form.visibility,
        },
      });

      setForm(EMPTY);
      await cols.reload();
    } catch (error) {
      setFormError(
        getApiErrorMessage(error, 'Could not create collection.')
      );
    }
  }

  async function handleCollectionDeleted(collectionId) {
    setOpenId((current) =>
      current === collectionId ? null : current
    );

    await cols.reload();
  }

  return (
    <div className="space-y-6">
      <Card>
        <h2 className="mb-3 text-lg font-semibold">
          Create collection
        </h2>

        <form onSubmit={guard(create)} className="space-y-2">
          <div className="flex flex-wrap items-end gap-3">
            <Label className="min-w-[160px] flex-1">
              Name
              <Input
                className="mt-1"
                value={form.name}
                onChange={set('name')}
                maxLength={NAME_MAX}
                required
              />
            </Label>

            <Label className="min-w-[160px] flex-1">
              Description
              <Input
                className="mt-1"
                value={form.description}
                onChange={set('description')}
                maxLength={DESC_MAX}
              />
            </Label>

            <Label>
              Visibility
              <Select
                className="mt-1"
                value={form.visibility}
                onChange={set('visibility')}
              >
                <option value="restricted">
                  Restricted (granted people only)
                </option>
                <option value="org">
                  Org-wide (everyone)
                </option>
              </Select>
            </Label>

            <Button variant="primary" type="submit">
              Create
            </Button>
          </div>

          {formError && (
            <p role="alert" className="text-sm text-danger">
              {formError}
            </p>
          )}
        </form>
      </Card>

      <Card>
        <Table
          head={['Name', 'Visibility', '']}
          rows={shared.map((collection) => [
            collection.name,
            <Pill key={`${collection.collection_id}-visibility`}>
              {collection.visibility}
            </Pill>,
            <Button
              key={`${collection.collection_id}-manage`}
              size="sm"
              onClick={() => setOpenId(collection.collection_id)}
            >
              Manage
            </Button>,
          ])}
        />

        {shared.length === 0 && (
          <p className="mt-2 text-sm text-muted">
            No shared collections yet.
          </p>
        )}
      </Card>

      {open && (
        <CollectionManager
          key={open.collection_id}
          collection={open}
          onDeleted={handleCollectionDeleted}
        />
      )}
    </div>
  );
}

function CollectionManager({ collection, onDeleted }) {
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

  // Fetched separately from the block above: once a collection delete is
  // in flight the collection may be marked 'deleting', at which point
  // /documents and /acl correctly 404 (collection_or_404 only recognizes
  // 'active' collections) -- but /cleanup-jobs deliberately doesn't depend
  // on the collection still existing, precisely so cleanup status stays
  // visible after the collection itself is gone from normal reads. Keeping
  // its own fetch means refreshing it never trips over the other two.
  const cleanupData = useAsync(
    () => api(`/collections/${id}/cleanup-jobs`),
    [id]
  );

  const [documentDeleteError, setDocumentDeleteError] = useState('');
  const [deletingDocumentId, setDeletingDocumentId] = useState(null);
  const [deleteError, setDeleteError] = useState('');
  const [isDeletingCollection, setIsDeletingCollection] = useState(false);
  const [retryingJobId, setRetryingJobId] = useState(null);
  const [retryError, setRetryError] = useState('');

  if (!data.data) {
    return (
      <Card>
        <Loading error={data.error} />
      </Card>
    );
  }

  const { docs, acl, users, groups } = data.data;
  // Jobs still needing attention -- a completed job isn't "unfinished
  // cleanup" and would just be noise here; this is specifically for
  // pending/in-progress/failed jobs that outlive the deleted document or
  // collection in the normal lists above.
  const unfinishedJobs = (cleanupData.data || []).filter(
    (job) => job.status !== 'completed'
  );

  async function retryJob(job) {
    if (retryingJobId !== null) return;
    setRetryError('');
    setRetryingJobId(job.job_id);

    try {
      const result = await api(
        `/collections/${id}/cleanup-jobs/${job.job_id}/retry`,
        { method: 'POST' }
      );

      if (job.job_type === 'collection' && result?.cleanup_status === 'completed') {
        // The collection's own cleanup just finished verifying -- it's
        // now fully gone, not merely excluded from search.
        await onDeleted(id);
        return;
      }

      await cleanupData.reload();
      if (job.job_type === 'document') {
        // A document-level job retry can also move chunk_count/status;
        // refresh the document list too (safe: the collection is still
        // active in this case).
        await data.reload();
      }
    } catch (error) {
      setRetryError(
        error?.message || 'Could not retry this cleanup job.'
      );
    } finally {
      setRetryingJobId(null);
    }
  }

  async function deleteDoc(document) {
    if (deletingDocumentId !== null) return;

    const confirmed = window.confirm(
      `Delete "${document.filename}"? This removes it from document search.`
    );

    if (!confirmed) return;

    setDocumentDeleteError('');
    setDeletingDocumentId(document.doc_id);

    try {
      const result = await api(
        `/collections/${id}/documents/${document.doc_id}`,
        { method: 'DELETE' }
      );

      await Promise.all([data.reload(), cleanupData.reload()]);

      if (result?.cleanup_pending) {
        setDocumentDeleteError(
          'The document is deleted and excluded from search. Its data ' +
          'cleanup is still in progress -- see Cleanup status below.'
        );
      }
    } catch (error) {
      setDocumentDeleteError(
        error?.message || 'Could not delete this document. Please retry.'
      );

      // Refresh to show the actual server state, but preserve the error.
      try {
        await data.reload();
      } catch {
        // Keep the original delete error visible.
      }
    } finally {
      setDeletingDocumentId(null);
    }
  }

  async function deleteCollection() {
    if (isDeletingCollection) return;

    setDeleteError('');

    const confirmed = window.confirm(
      `Permanently delete collection "${collection.name}" and all its documents, access rules, and related data? This action cannot be undone.`
    );

    if (!confirmed) return;

    setIsDeletingCollection(true);

    try {
      const result = await api(`/collections/${id}`, { method: 'DELETE' });

      if (result?.cleanup_status === 'completed') {
        await onDeleted(id);
        return;
      }

      // Already excluded from search and from the main collections list,
      // but physical cleanup didn't finish inline. Stay open so its job
      // (now in the list below) can be checked on and retried, instead of
      // the admin losing track of it the moment this view would close.
      setDeleteError(
        'Collection deleted and excluded from search. Physical data ' +
        'cleanup is still in progress -- see Cleanup status below.'
      );
      await cleanupData.reload();
    } catch (error) {
      setDeleteError(
        error?.message || 'Could not delete this collection.'
      );
    } finally {
      setIsDeletingCollection(false);
    }
  }

  return (
    <Card className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">
            Manage: {collection.name}
          </h2>

          {collection.description && (
            <p className="mt-1 text-sm text-muted">
              {collection.description}
            </p>
          )}
        </div>

        <Button
          variant="danger"
          onClick={guard(deleteCollection)}
          disabled={isDeletingCollection}
        >
          {isDeletingCollection ? 'Deleting…' : 'Delete collection'}
        </Button>
      </div>

      {deleteError && (
        <p role="alert" className="text-sm text-danger">
          {deleteError}
        </p>
      )}

      <div>
        <h3 className="mb-2 font-medium">Documents</h3>

        {documentDeleteError && (
          <p role="alert" className="mb-3 text-sm text-danger">
            {documentDeleteError}
          </p>
        )}

        {docs.length === 0 ? (
          <p className="text-sm text-muted">
            No documents yet. Upload from the Upload page.
          </p>
        ) : (
          <Table
            head={[
              'File',
              'Version',
              'Status',
              'Chunks',
              'Findings',
              '',
            ]}
            rows={docs.map((document) => [
              document.filename,
              `v${document.version}`,
              document.status,
              document.chunk_count,
              Array.isArray(document.findings)
                ? document.findings.length
                : 0,
              <Button
                key={`${document.doc_id}-delete`}
                size="sm"
                variant="danger"
                disabled={deletingDocumentId !== null}
                onClick={guard(() => deleteDoc(document))}
              >
                {deletingDocumentId === document.doc_id
                  ? 'Deleting…'
                  : 'Delete'}
              </Button>,
            ])}
          />
        )}
      </div>

      {unfinishedJobs.length > 0 && (
        <div>
          <h3 className="mb-2 font-medium">Cleanup status</h3>
          <p className="mb-2 text-sm text-muted">
            Deleted documents and collections are removed from search
            immediately; this shows any physical cleanup that's still
            pending or needs attention, even after the item itself has
            disappeared from the lists above.
          </p>

          {retryError && (
            <p role="alert" className="mb-3 text-sm text-danger">
              {retryError}
            </p>
          )}

          <Table
            head={['Type', 'Target', 'Status', 'Attempts', 'Last error', '']}
            rows={unfinishedJobs.map((job) => [
              job.job_type,
              job.target_id,
              <Pill
                key={`${job.job_id}-status`}
                tone={job.status === 'failed' ? 'danger' : 'default'}
              >
                {job.status}
              </Pill>,
              `${job.attempts}/${job.max_attempts}`,
              job.last_error || '—',
              <Button
                key={`${job.job_id}-retry`}
                size="sm"
                variant="subtle"
                disabled={retryingJobId !== null || job.status === 'in_progress'}
                onClick={guard(() => retryJob(job))}
              >
                {retryingJobId === job.job_id ? 'Retrying…' : 'Retry'}
              </Button>,
            ])}
          />
        </div>
      )}

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

function AccessForm({
  collectionId,
  acl,
  users,
  groups,
  onSaved,
}) {
  const [userIds, setUserIds] = useState(acl.user_ids);
  const [groupIds, setGroupIds] = useState(acl.group_ids);
  const [visibility, setVisibility] = useState(acl.visibility);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  const toggle = (list, setList, value) => {
    setList((current) =>
      current.includes(value)
        ? current.filter((item) => item !== value)
        : [...current, value]
    );
  };

  async function save() {
    if (saving) return;

    setError('');
    setSaving(true);

    try {
      await api(`/collections/${collectionId}/acl`, {
        method: 'PUT',
        body: {
          user_ids: userIds,
          group_ids: groupIds,
          visibility,
        },
      });

      await onSaved();
    } catch (err) {
      setError(err?.message || 'Could not save collection access.');
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-4">
      <h3 className="font-medium">Who can read it</h3>

      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}

      <Label>
        Visibility
        <Select
          className="mt-1 max-w-xs"
          value={visibility}
          onChange={(event) => setVisibility(event.target.value)}
        >
          <option value="restricted">Restricted</option>
          <option value="org">Org-wide (everyone)</option>
        </Select>
      </Label>

      <div>
        <p className="mb-1 text-xs text-muted">Users</p>

        <div className="grid gap-1 sm:grid-cols-3">
          {users.map((user) => (
            <Check
              key={user.user_id}
              label={user.username}
              checked={userIds.includes(user.user_id)}
              onChange={() =>
                toggle(userIds, setUserIds, user.user_id)
              }
            />
          ))}
        </div>
      </div>

      <div>
        <p className="mb-1 text-xs text-muted">Groups</p>

        <div className="grid gap-1 sm:grid-cols-3">
          {groups.map((group) => (
            <Check
              key={group.group_id}
              label={group.name}
              checked={groupIds.includes(group.group_id)}
              onChange={() =>
                toggle(groupIds, setGroupIds, group.group_id)
              }
            />
          ))}
        </div>
      </div>

      <Button
        variant="primary"
        onClick={guard(save)}
        disabled={saving}
      >
        {saving ? 'Saving…' : 'Save access'}
      </Button>
    </div>
  );
}
