
export const API =
  import.meta.env.VITE_API_URL ||
  (import.meta.env.DEV ? 'http://localhost:8000' : '');

export const getToken = () => {
  try {
    return localStorage.getItem('token');
  } catch {
    return null;
  }
};

export const saveToken = (token) => {
  try {
    if (token) localStorage.setItem('token', token);
    else localStorage.removeItem('token');
  } catch {
    throw new Error('Your browser could not save your session. Check your browser storage settings.');
  }
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

  if (Array.isArray(detail)) {
    const messages = detail.map((item) => {
      const location = Array.isArray(item?.loc) ? item.loc : [];
      const field = String(location[location.length - 1] || '');
      const label = FIELD_LABELS[field];
      const type = String(item?.type || '');

      if (type === 'missing' || item?.msg === 'Field required') {
        return label
          ? `Please enter your ${label}.`
          : 'Please complete all required fields.';
      }

      if (field === 'email') {
        return 'Please enter a valid email address.';
      }

      if (field === 'password') {
        return 'Please check your password and try again.';
      }

      return 'Some information is invalid. Please review the form.';
    });

    return [...new Set(messages)].join(' ');
  }

  // These are endpoint-generated messages, not raw validation objects.
  if (
    typeof detail === 'string' &&
    status >= 400 &&
    status < 500
  ) {
    return detail;
  }

  const messages = {
    400: 'The request could not be completed. Please check your details.',
    401: 'Those sign-in details were not recognised.',
    403: 'You do not have permission to do that.',
    404: 'The requested item could not be found.',
    409: 'That account or item already exists.',
    422: 'Some information is missing or invalid. Please review the form.',
    429: 'Too many requests. Please wait a moment and try again.',
  };

  if (messages[status]) return messages[status];

  if (status >= 500) {
    return 'Something went wrong on the server. Please try again later.';
  }

  return 'Something went wrong. Please try again.';
}

function networkError() {
  return new Error(
    'We could not connect to the service. Check your internet connection and try again.'
  );
}

async function request(path, options = {}) {
  try {
    return await fetch(`${API}${path}`, options);
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    throw networkError();
  }
}

function handleExpiredSession(path, response, token) {
  if (
    response.status === 401 &&
    token &&
    !path.startsWith('/auth/login') &&
    !path.startsWith('/auth/register')
  ) {
    saveToken(null);
    window.dispatchEvent(new Event('auth:expired'));
    return true;
  }

  return false;
}

export async function api(
  path,
  {
    method = 'GET',
    body,
    form,
    raw = false,
    signal,
  } = {}
) {
  const token = getToken();
  const headers = {};

  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }

  let payload;

  if (form) {
    payload = form;
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }

  const res = await request(path, {
    method,
    headers,
    body: payload,
    signal,
  });

  if (handleExpiredSession(path, res, token)) {
    throw new Error('Your session expired. Please sign in again.');
  }

  if (raw) return res;

  const data = await readJson(res).catch(() => null);

  if (!res.ok) {
    throw new Error(errorMessage(data, res.status));
  }

  return data;
}

/**
 * Consume the application's newline-delimited JSON stream.
 * Supported events: trace, reset, token, final, error.
 */
async function consumeEventStream(response, onEvent, signal) {
  if (!response.body) {
    throw new Error('The server did not provide a response stream.');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();

  let buffer = '';
  let receivedFinal = false;

  function handleLine(line) {
    const trimmed = line.trim();
    if (!trimmed) return;

    let event;

    try {
      event = JSON.parse(trimmed);
    } catch {
      onEvent({
        type: 'warning',
        message: 'Part of the response could not be read.',
      });
      return;
    }

    if (
      !event ||
      typeof event !== 'object' ||
      typeof event.type !== 'string'
    ) {
      return;
    }

    if (event.type === 'final') {
      receivedFinal = true;
    }

    if (event.type === 'error') {
      throw new Error(
        typeof event.detail === 'string'
          ? event.detail
          : 'The assistant could not complete this response.'
      );
    }

    onEvent(event);
  }

  try {
    while (true) {
      if (signal?.aborted) {
        throw new DOMException(
          'The request was cancelled.',
          'AbortError'
        );
      }

      const { value, done } = await reader.read();

      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      let index;

      while ((index = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, index);
        buffer = buffer.slice(index + 1);
        handleLine(line);
      }
    }

    buffer += decoder.decode();

    if (buffer.trim()) {
      handleLine(buffer);
    }

    if (!receivedFinal) {
      throw new Error(
        'The connection ended before the answer was complete. Please try again.'
      );
    }
  } finally {
    try {
      await reader.cancel();
    } catch {
      // The stream may already have closed.
    }
  }
}

export async function streamChat(
  threadId,
  body,
  onEvent,
  signal
) {
  const res = await api(
    `/threads/${encodeURIComponent(threadId)}/chat/stream`,
    {
      method: 'POST',
      body,
      raw: true,
      signal,
    }
  );

  if (!res.ok) {
    const data = await readJson(res).catch(() => null);
    throw new Error(errorMessage(data, res.status));
  }

  return consumeEventStream(res, onEvent, signal);
}

export async function guestUpload(files) {
  const form = new FormData();

  for (const file of files) {
    form.append('files', file);
  }

  const res = await request('/chat/guest/upload', {
    method: 'POST',
    body: form,
  });

  const data = await readJson(res).catch(() => null);

  if (!res.ok) {
    throw new Error(errorMessage(data, res.status));
  }

  return data;
}

export async function guestStreamChat(
  collectionId,
  question,
  onEvent,
  signal
) {
  const res = await request('/chat/guest/stream', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      collection_id: collectionId,
      question,
    }),
    signal,
  });

  if (!res.ok) {
    const data = await readJson(res).catch(() => null);
    throw new Error(errorMessage(data, res.status));
  }

  return consumeEventStream(res, onEvent, signal);
}

export function guestRelease(collectionId) {
  if (!collectionId) return Promise.resolve();

  return request(
    `/chat/guest/collection/${encodeURIComponent(collectionId)}`,
    { method: 'DELETE' }
  ).catch(() => {
    // Best effort. The backend also deletes guest data after its TTL.
  });
}
