const BASE_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

async function handle(res) {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {
      // response wasn't JSON — keep statusText
    }
    throw new Error(detail);
  }
  return res.json();
}

// /auth/login expects OAuth2 form-encoded fields, NOT JSON — this is a
// FastAPI OAuth2PasswordRequestForm requirement on the backend.
export function apiLogin(username, password) {
  const body = new URLSearchParams({ username, password });
  return fetch(`${BASE_URL}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  }).then(handle);
}

export function apiRegister(username, password, orgName) {
  return fetch(`${BASE_URL}/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password, org_name: orgName || undefined }),
  }).then(handle);
}

// Generic authenticated fetch for every other endpoint.
export function apiFetch(path, { token, headers, ...options } = {}) {
  const mergedHeaders = { ...(headers || {}) };
  if (token) mergedHeaders.Authorization = `Bearer ${token}`;
  return fetch(`${BASE_URL}${path}`, { ...options, headers: mergedHeaders }).then(handle);
}

// ---- Org admin: users ----
export function listOrgUsers(token) {
  return apiFetch("/org/users", { token });
}

export function createOrgUser(token, { username, password, role }) {
  return apiFetch("/org/users", {
    token,
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password, role }),
  });
}

export function deleteOrgUser(token, userId) {
  return apiFetch(`/org/users/${userId}`, { token, method: "DELETE" });
}

// ---- Org admin: groups ----
export function listOrgGroups(token) {
  return apiFetch("/org/groups", { token });
}

export function createOrgGroup(token, name) {
  return apiFetch("/org/groups", {
    token,
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
}

export function addGroupMember(token, groupId, userId) {
  return apiFetch(`/org/groups/${groupId}/members`, {
    token,
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId }),
  });
}

export function removeGroupMember(token, groupId, userId) {
  return apiFetch(`/org/groups/${groupId}/members/${userId}`, { token, method: "DELETE" });
}

export function deleteOrgGroup(token, groupId) {
  return apiFetch(`/org/groups/${groupId}`, { token, method: "DELETE" });
}