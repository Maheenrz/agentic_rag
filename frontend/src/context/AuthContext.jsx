import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { apiFetch, apiLogin, apiRegister } from "../api/client";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [token, setToken] = useState(() => localStorage.getItem("token"));
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  const loadUser = useCallback(async (currentToken) => {
    if (!currentToken) {
      setUser(null);
      setLoading(false);
      return;
    }
    try {
      const me = await apiFetch("/me", { token: currentToken });
      setUser(me);
    } catch {
      // token expired, invalid, or account deactivated server-side
      localStorage.removeItem("token");
      setToken(null);
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadUser(token);
  }, [token, loadUser]);

  const login = async (username, password) => {
    const { access_token } = await apiLogin(username, password);
    localStorage.setItem("token", access_token);
    setToken(access_token);
  };

  const register = async (username, password, orgName) => {
    await apiRegister(username, password, orgName);
    await login(username, password); // register doesn't return a token itself
  };

  const logout = () => {
    localStorage.removeItem("token");
    setToken(null);
    setUser(null);
  };

  return (
    <AuthContext.Provider value={{ user, token, loading, login, register, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}