import { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { api, getToken, saveToken } from '../lib/api';

const AppContext = createContext(null);

export function AppProvider({ children }) {
  const [me, setMe] = useState(null);
  const [threads, setThreads] = useState([]);
  const [collections, setCollections] = useState([]);
  const [selected, setSelected] = useState([]);   // collection ids; empty = search everything
  const [ready, setReady] = useState(false);

  const refreshThreads = useCallback(async () => {
    setThreads(await api('/threads'));
  }, []);

  const refreshCollections = useCallback(async () => {
    const list = await api('/collections');
    setCollections(list);
    setSelected((s) => s.filter((id) => list.some((c) => c.collection_id === id)));
  }, []);

  const loadSession = useCallback(async () => {
    setMe(await api('/me'));
    await Promise.all([refreshThreads(), refreshCollections()]);
  }, [refreshThreads, refreshCollections]);

  const logout = useCallback(() => {
    saveToken(null);
    setMe(null);
    setThreads([]);
    setCollections([]);
    setSelected([]);
  }, []);

  const login = useCallback(async (username, password) => {
    const data = await api('/auth/login', {
      method: 'POST',
      form: new URLSearchParams({ username, password }),
    });
    saveToken(data.access_token);
    await loadSession();
  }, [loadSession]);

  const register = useCallback(async (username, password, org_name) => {
    await api('/auth/register', {
      method: 'POST',
      body: { username, password, org_name: org_name || null },
    });
    await login(username, password);
  }, [login]);

  const toggleCollection = useCallback((id) => {
    setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));
  }, []);

  // Restore an existing session when the page loads.
  useEffect(() => {
    if (!getToken()) {
      setReady(true);
      return;
    }
    loadSession()
      .catch(() => saveToken(null))
      .finally(() => setReady(true));
  }, [loadSession]);

  // api() fires this on any 401, which ends the session.
  useEffect(() => {
    window.addEventListener('auth:expired', logout);
    return () => window.removeEventListener('auth:expired', logout);
  }, [logout]);

  const value = {
    me, threads, collections, selected, toggleCollection,
    refreshThreads, refreshCollections, login, register, logout, ready,
  };

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export function useApp() {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error('useApp must be used inside AppProvider');
  return ctx;
}