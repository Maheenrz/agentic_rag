import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useApp } from '../context/AppContext';
import { Button, Input, Label } from '../components/ui';

const USERNAME_RE = /^[A-Za-z0-9_.@-]{3,64}$/;   // same rule as the backend
const MIN_PASSWORD_CHARS = 8;
const MAX_PASSWORD_BYTES = 72;                    // bcrypt limit, counted in bytes

function validateRegister(username, password) {
  if (!USERNAME_RE.test(username)) {
    return 'Username must be 3–64 characters: letters, digits, _ . @ -';
  }
  if (password.length < MIN_PASSWORD_CHARS) {
    return `Password must be at least ${MIN_PASSWORD_CHARS} characters.`;
  }
  if (new TextEncoder().encode(password).length > MAX_PASSWORD_BYTES) {
    return `Password is too long (max ${MAX_PASSWORD_BYTES} bytes). Accented or non-Latin characters take more than one byte each.`;
  }
  return '';
}

export default function LoginPage({ mode = 'login' }) {
  const { login, register } = useApp();
  const navigate = useNavigate();
  const isLogin = mode === 'login';

  const [form, setForm] = useState({ username: '', password: '', org: '' });
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value });

  async function submit(e) {
    e.preventDefault();
    setError('');

    const username = form.username.trim();
    if (!isLogin) {
      const problem = validateRegister(username, form.password);
      if (problem) {
        setError(problem);
        return;
      }
    }

    setBusy(true);
    try {
      if (isLogin) await login(username, form.password);
      else await register(username, form.password, form.org.trim());
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen flex-col">
      <div className="px-6 py-6">
        <Link to="/" className="font-display text-2xl">Docs Assistant</Link>
      </div>

      <div className="flex flex-1 items-center justify-center px-5 pb-20">
        <div className="w-full max-w-sm">
          <h1 className="font-display text-5xl leading-tight">
            {isLogin ? 'Welcome back.' : 'Create your organisation.'}
          </h1>
          <p className="mb-9 mt-3 text-sm text-muted">
            {isLogin
              ? 'Sign in to ask your documents.'
              : 'You will be the admin. You can add people and collections after this.'}
          </p>

          <form onSubmit={submit} className="space-y-4">
            <Label>
              Username
              <Input className="mt-1.5" autoComplete="username" value={form.username} onChange={set('username')} />
              {!isLogin && (
                <span className="mt-1.5 block text-xs font-normal text-muted">
                  3–64 characters: letters, digits, _ . @ -
                </span>
              )}
            </Label>

            <Label>
              Password
              <Input
                className="mt-1.5"
                type="password"
                autoComplete={isLogin ? 'current-password' : 'new-password'}
                value={form.password}
                onChange={set('password')}
              />
              {!isLogin && (
                <span className="mt-1.5 block text-xs font-normal text-muted">
                  At least 8 characters, at most 72 bytes.
                </span>
              )}
            </Label>

            {!isLogin && (
              <Label>
                Organisation name
                <Input className="mt-1.5" placeholder="e.g. Acme Ltd" value={form.org} onChange={set('org')} />
              </Label>
            )}

            <Button variant="primary" type="submit" disabled={busy} className="w-full py-3">
              {busy ? 'Please wait…' : isLogin ? 'Sign in' : 'Create account'}
            </Button>

            {error && <p className="text-sm text-danger">{error}</p>}
          </form>

          <button
            type="button"
            onClick={() => {
              setError('');
              navigate(isLogin ? '/register' : '/login');
            }}
            className="mt-8 text-sm text-muted transition hover:text-ink"
          >
            {isLogin ? 'New organisation? Create an account' : 'Already have an account? Sign in'}
          </button>
        </div>
      </div>
    </div>
  );
}