// All browser API calls use the same-origin /api path. Vercel and Vite proxy
// this path to FastAPI, so the browser can send an HttpOnly session cookie.
export const API = '/api';

export async function readJson(res) {
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

const FIELD_LABELS = {
  email: 'email address',
  username: 'username',
  password: 'password',
  org_name: 'organisation name',
  identifier: 'username or email',
  token: 'reset or invitation link',
};

export function errorMessage(data, status) {
  const detail = data?.detail;
  if (typeof detail === 'string') return detail;

  if (Array.isArray(detail)) {
    const messages = detail.map((item) => {
      const location = Array.isArray(item?.loc) ? item.loc : [];
      const field = location.length ? String(location[location.length - 1]) : '';
      const label = FIELD_LABELS[field] || (field ? field.replaceAll('_', ' ') : 'field');
      const type = item?.type || '';

      // Never display FastAPI input/context values: they can contain passwords.
      if (type === 'missing' || item?.msg === 'Field required') return `Please enter your ${label}.`;
      if (field === 'email' && (type.includes('email') || /valid email/i.test(item?.msg || ''))) {
        return 'Please enter a valid email address.';
      }
      if (field === 'password') return 'The password does not meet the requirements. Please check it and try again.';
      if (item?.msg && !/value error|input should/i.test(item.msg)) {
        return `${label.charAt(0).toUpperCase()}${label.slice(1)}: ${item.msg}`;
      }
      return 'Some information is missing or invalid. Please review the form.';
    });
    return [...new Set(messages)].join(' ');
  }

  if (status === 400) return 'The request could not be completed. Please check your details.';
  if (status === 401) return 'Your session may have expired. Please sign in again.';
  if (status === 403) return 'You do not have permission to do that.';
  if (status === 404) return 'The requested item could not be found.';
  if (status === 409) return 'That account or item already exists.';
  if (status === 422) return 'Some information is missing or invalid. Please review the form.';
  if (status >= 500) return 'The server encountered a problem. Please try again shortly.';
  return `Request failed (${status}). Please try again.`;
}

/**
 * api('/path', { method, body, form, raw })
 * Authentication is cookie-based. The browser sends the HttpOnly cookie;
 * JavaScript never reads or stores the JWT.
 */
export async function api(path, { method = 'GET', body, form, raw = false, signal } = {}) {
  const headers = {};
  let payload;

  if (form) {
    payload = form;
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }

  let res;
  try {
    res = await fetch(API + path, {
      method,
      headers,
      body: payload,
      credentials: 'include',
      signal,
    });
  } catch {
    throw new Error('Cannot reach the server. Please check your connection and try again.');
  }

  const isAuthRequest = path.startsWith('/auth/login') || path.startsWith('/auth/logout');
  if (res.status === 401 && !isAuthRequest) {
    window.dispatchEvent(new Event('auth:expired'));
    throw new Error('Your session expired. Please sign in again.');
  }

  if (raw) return res;
  const data = await readJson(res);
  if (!res.ok) throw new Error(errorMessage(data, res.status));
  return data;
}

/** Streams a chat answer. The backend sends newline-separated JSON events. */
export async function streamChat(threadId, body, onEvent) {
  const res = await api(`/threads/${threadId}/chat/stream`, { method: 'POST', body, raw: true });
  if (!res.ok) throw new Error(errorMessage(await readJson(res), res.status));

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const handle = (line) => { if (line.trim()) onEvent(JSON.parse(line)); };

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

export async function guestUpload(files) {
  const form = new FormData();
  for (const file of files) form.append('files', file);
  const res = await fetch(API + '/chat/guest/upload', { method: 'POST', body: form, credentials: 'include' });
  if (!res.ok) throw new Error(errorMessage(await readJson(res).catch(() => null), res.status));
  return readJson(res);
}

export async function guestStreamChat(collectionId, question, onEvent, signal) {
  const res = await fetch(API + '/chat/guest/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ collection_id: collectionId, question }),
    credentials: 'include',
    signal,
  });
  if (!res.ok) throw new Error(errorMessage(await readJson(res).catch(() => null), res.status));

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
    reader.cancel().catch(() => {});
  }
}

export function guestRelease(collectionId) {
  return fetch(`${API}/chat/guest/collection/${encodeURIComponent(collectionId)}`, {
    method: 'DELETE',
    credentials: 'include',
  }).catch(() => {});
}