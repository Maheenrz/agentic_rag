export const API = import.meta.env.VITE_API_URL || 'http://localhost:8000';

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