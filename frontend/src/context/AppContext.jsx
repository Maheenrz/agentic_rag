import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from 'react';
import { api, getToken, saveToken } from '../lib/api';

const AppContext = createContext(null);

export function AppProvider({ children }) {
  const [me, setMe] = useState(null);
  const [threads, setThreads] = useState([]);
  const [collections, setCollections] = useState([]);
  const [selected, setSelected] = useState([]);
  const [ready, setReady] = useState(false);

  const refreshThreads = useCallback(async () => {
    const result = await api('/threads');
    setThreads(Array.isArray(result) ? result : []);
  }, []);

  const refreshCollections = useCallback(async () => {
    const result = await api('/collections');
    const list = Array.isArray(result) ? result : [];

    setCollections(list);
    setSelected((current) =>
      current.filter((id) =>
        list.some((collection) => collection.collection_id === id)
      )
    );
  }, []);

  const loadSession = useCallback(async () => {
    const user = await api('/me');
    setMe(user);

    await Promise.all([
      refreshThreads(),
      refreshCollections(),
    ]);

    return user;
  }, [refreshThreads, refreshCollections]);

  const logout = useCallback(() => {
    saveToken(null);
    setMe(null);
    setThreads([]);
    setCollections([]);
    setSelected([]);
  }, []);

  const login = useCallback(async (identifier, password) => {
    const data = await api('/auth/login', {
      method: 'POST',
      body: { identifier, password },
    });

    if (!data?.access_token) {
      throw new Error('Login did not return an access token.');
    }

    saveToken(data.access_token);

    try {
      await loadSession();
    } catch (error) {
      saveToken(null);
      setMe(null);
      setThreads([]);
      setCollections([]);
      setSelected([]);
      throw error;
    }
  }, [loadSession]);

  const register = useCallback(async ({
    username,
    email,
    password,
    org,
  }) => {
    await api('/auth/register', {
      method: 'POST',
      body: {
        username,
        email,
        password,
        org_name: org || null,
      },
    });

    await login(username, password);
  }, [login]);

  const toggleCollection = useCallback((id) => {
    setSelected((current) =>
      current.includes(id)
        ? current.filter((item) => item !== id)
        : [...current, id]
    );
  }, []);

  useEffect(() => {
    let active = true;

    async function restoreSession() {
      if (!getToken()) {
        if (active) setReady(true);
        return;
      }

      try {
        await loadSession();
      } catch {
        saveToken(null);
        if (active) {
          setMe(null);
          setThreads([]);
          setCollections([]);
          setSelected([]);
        }
      } finally {
        if (active) setReady(true);
      }
    }

    restoreSession();

    return () => {
      active = false;
    };
  }, [loadSession]);

  useEffect(() => {
    window.addEventListener('auth:expired', logout);
    return () => {
      window.removeEventListener('auth:expired', logout);
    };
  }, [logout]);

  const value = {
    me,
    threads,
    collections,
    selected,
    toggleCollection,
    refreshThreads,
    refreshCollections,
    login,
    register,
    logout,
    ready,
  };

  return (
    <AppContext.Provider value={value}>
      {children}
    </AppContext.Provider>
  );
}

export function useApp() {
  const context = useContext(AppContext);

  if (!context) {
    throw new Error('useApp must be used inside AppProvider');
  }

  return context;
}
