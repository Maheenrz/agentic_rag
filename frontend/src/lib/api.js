export const API = import.meta.env.VITE_API_URL || (import.meta.env.DEV ? 'http://localhost:8000' : '');

export const getToken = () => localStorage.getItem('token');

export const saveToken = (token) => {
  if (token) localStorage.setItem('token', token);
  else localStorage.removeItem('token');
};

export async function readJson(res) {
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export function errorMessage(data, status) {
  const detail = data?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg).join('; ');
  return `Request failed (${status})`;
}

/**
 * api('/path', { method, body, form, raw })
 *  - body: plain object, sent as JSON
 *  - form: URLSearchParams or FormData
 *  - raw: return the Response instead of parsed JSON (used for uploads and streaming)
 */
export async function api(path, { method = 'GET', body, form, raw = false } = {}) {
  const token = getToken();
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;

  let payload;
  if (form) {
    payload = form;
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }

  let res;
  try {
    res = await fetch(API + path, { method, headers, body: payload });
  } catch {
    throw new Error(`Cannot reach the server at ${API}. Is uvicorn running?`);
  }

  if (res.status === 401 && token && !path.startsWith('/auth/login')) {
    window.dispatchEvent(new Event('auth:expired'));
    throw new Error('Your session expired. Please sign in again.');
  }

  if (raw) return res;

  const data = await readJson(res);
  if (!res.ok) throw new Error(errorMessage(data, res.status));
  return data;
}

/**
 * Streams a chat answer. The backend sends newline-separated JSON events:
 * trace, reset, token, final, error. onEvent is called for each one.
 */
export async function streamChat(threadId, body, onEvent) {
  const res = await api(`/threads/${threadId}/chat/stream`, { method: 'POST', body, raw: true });
  if (!res.ok) throw new Error(errorMessage(await readJson(res), res.status));

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  const handle = (line) => {
    if (line.trim()) onEvent(JSON.parse(line));
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf('\n')) >= 0) {
      handle(buffer.slice(0, idx));
      buffer = buffer.slice(idx + 1);
    }
  }
  handle(buffer + decoder.decode());
}
/** Upload a file for a guest session. Returns {collection_id, chunks, expires_at, ttl_minutes}. */
export async function guestUpload(files) {
  const form = new FormData();
  for (const f of files) form.append('files', f);

  const res = await fetch(API + '/chat/guest/upload', { method: 'POST', body: form });
  if (!res.ok) {
    const data = await readJson(res).catch(() => null);
    throw new Error(errorMessage(data, res.status));
  }
  return readJson(res);
}


/**
 * Ask a question about the guest's uploaded document. Streams like streamChat.
 * Pass a `signal` (from an AbortController) to cancel the request when the
 * modal closes — the server sees the disconnect and stops generating.
 */
export async function guestStreamChat(collectionId, question, onEvent, signal) {
  const res = await fetch(API + '/chat/guest/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ collection_id: collectionId, question }),
    signal,
  });

  if (!res.ok) {
    const data = await readJson(res).catch(() => null);
    throw new Error(errorMessage(data, res.status));
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  const handle = (line) => { if (line.trim()) onEvent(JSON.parse(line)); };

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf('\n')) >= 0) {
        handle(buffer.slice(0, idx));
        buffer = buffer.slice(idx + 1);
      }
    }
    handle(buffer + decoder.decode());
  } finally {
    reader.cancel().catch(() => {});   // ensure the connection closes even on throw
  }
}

/** Clean up on modal close. Best-effort — the server deletes on TTL anyway. */
export function guestRelease(collectionId) {
  return fetch(API + '/chat/guest/collection/' + collectionId, { method: 'DELETE' })
    .catch(() => {});   // best-effort
}