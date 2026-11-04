import { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { api } from '../lib/api';

const AppContext = createContext(null);

export function AppProvider({ children }) {
  const [me, setMe] = useState(null);
  const [threads, setThreads] = useState([]);
  const [collections, setCollections] = useState([]);
  const [selected, setSelected] = useState([]);
  const [ready, setReady] = useState(false);

  const clearSession = useCallback(() => {
    setMe(null);
    setThreads([]);
    setCollections([]);
    setSelected([]);
  }, []);

  const refreshThreads = useCallback(async () => {
    const result = await api('/threads');
    setThreads(Array.isArray(result) ? result : []);
  }, []);

  const refreshCollections = useCallback(async () => {
    const result = await api('/collections');
    const list = Array.isArray(result) ? result : [];
    setCollections(list);
    setSelected((current) => current.filter((id) => list.some((item) => item.collection_id === id)));
  }, []);

  const loadSession = useCallback(async () => {
    // /me validates the HttpOnly cookie; no token is read by frontend JS.
    const user = await api('/me');
    setMe(user);
    await Promise.all([refreshThreads(), refreshCollections()]);
    return user;
  }, [refreshThreads, refreshCollections]);

  const login = useCallback(async (identifier, password) => {
    // Backend must set the HttpOnly cookie on this response.
    await api('/auth/login', { method: 'POST', body: { identifier, password } });
    try {
      await loadSession();
    } catch (error) {
      clearSession();
      // Clear a cookie that may have been set if session initialization failed.
      await api('/auth/logout', { method: 'POST' }).catch(() => {});
      throw error;
    }
  }, [loadSession, clearSession]);

  const register = useCallback(async ({ username, email, password, org }) => {
    await api('/auth/register', {
      method: 'POST',
      body: { username, email, password, org_name: org || null },
    });
    await login(username, password);
  }, [login]);

  const logout = useCallback(async () => {
    try {
      await api('/auth/logout', { method: 'POST' });
    } finally {
      clearSession();
    }
  }, [clearSession]);

  const toggleCollection = useCallback((id) => {
    setSelected((current) => current.includes(id)
      ? current.filter((item) => item !== id)
      : [...current, id]);
  }, []);

  useEffect(() => {
    let active = true;
    async function restoreSession() {
      try {
        await loadSession();
      } catch {
        clearSession();
      } finally {
        if (active) setReady(true);
      }
    }
    restoreSession();
    return () => { active = false; };
  }, [loadSession, clearSession]);

  useEffect(() => {
    const onExpired = () => clearSession();
    window.addEventListener('auth:expired', onExpired);
    return () => window.removeEventListener('auth:expired', onExpired);
  }, [clearSession]);

  const value = {
    me, threads, collections, selected, toggleCollection,
    refreshThreads, refreshCollections, login, register, logout, ready,
  };

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export function useApp() {
  const context = useContext(AppContext);
  if (!context) throw new Error('useApp must be used inside AppProvider');
  return context;
}
