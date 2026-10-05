import { useRef, useState } from 'react';
import { useApp } from '../context/AppContext';
import { api, errorMessage, readJson } from '../lib/api';
import { DOC_ACCEPT } from '../lib/constants';
import { Button, Card, Label, Select, cx } from '../components/ui';

const OK_STATUSES = ['added', 'new_version', 'unchanged'];

async function sendFiles(path, files, extra = {}) {
  const form = new FormData();
  files.forEach((f) => form.append('files', f));
  Object.entries(extra).forEach(([k, v]) => form.append(k, v));
  const res = await api(path, { method: 'POST', form, raw: true });
  return { status: res.status, data: await readJson(res) };
}

export default function UploadPage() {
  const { me, threads, collections, refreshCollections } = useApp();
  const isAdmin = me.role === 'admin';
  const fileInput = useRef(null);

  const [target, setTarget] = useState('personal');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [results, setResults] = useState([]);

  const sharedCollections = collections.filter((c) => !c.owner_user_id);

  async function upload() {
    const files = [...(fileInput.current?.files || [])];
    if (!files.length) {
      setError('Choose one or more files first.');
      return;
    }

    setBusy(true);
    setError('');
    setResults([]);

    try {
      let path;
      if (target === 'personal') {
        // Private files attach to one of your chats. Reuse the latest one, or create one.
        const threadId = threads[0]?.thread_id || (await api('/threads', { method: 'POST' })).thread_id;
        path = `/threads/${threadId}/upload`;
      } else {
        path = `/collections/${target}/documents`;
      }

      let { status, data } = await sendFiles(path, files);

      const canOverride = isAdmin || target === 'personal';
      if (
        status === 422 &&
        canOverride &&
        confirm('Some files look like prompt-injection attempts. Upload anyway? The override is recorded in the audit log.')
      ) {
        ({ status, data } = await sendFiles(path, files, { allow_flagged: 'true' }));
      }

      if (!data?.results) throw new Error(errorMessage(data, status));

      setResults(data.results);
      fileInput.current.value = '';
      refreshCollections().catch(() => {});
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto w-full max-w-2xl space-y-6 overflow-y-auto p-6">
      <div>
        <h1 className="text-2xl font-semibold">Upload documents</h1>
        <p className="text-sm text-muted">
          PDF, Word, Excel, HTML, Markdown or text. Every file is scanned for prompt-injection content before it is indexed.
        </p>
      </div>

      <Card className="space-y-4">
        <Label>
          Where should it go?
          <Select className="mt-1 w-full" value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="personal">My private documents</option>
            {isAdmin &&
              sharedCollections.map((c) => (
                <option key={c.collection_id} value={c.collection_id}>{c.name}</option>
              ))}
          </Select>
        </Label>

        <input
          ref={fileInput}
          type="file"
          multiple
          accept={DOC_ACCEPT}
          className="block w-full text-sm text-muted file:mr-3 file:rounded-lg file:border-0 file:bg-surface-2 file:px-3 file:py-2 file:text-ink hover:file:bg-border"
        />

        <Button variant="primary" disabled={busy} onClick={upload}>
          {busy ? 'Uploading…' : 'Upload'}
        </Button>

        {error && <p className="text-sm text-danger">{error}</p>}
      </Card>

      {results.length > 0 && (
        <Card>
          <ul className="space-y-2 text-sm">
            {results.map((r, i) => (
              <li key={i} className={cx(OK_STATUSES.includes(r.status) ? 'text-ink' : 'text-danger')}>
                {r.filename}: {r.status}
                {r.chunks ? ` (${r.chunks} chunks)` : ''}
                {r.error ? ` — ${r.error}` : ''}
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}