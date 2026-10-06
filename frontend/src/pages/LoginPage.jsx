import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ArrowRight } from 'lucide-react';
import { useApp } from '../context/AppContext';
import { Button, Input, Label } from '../components/ui';

export default function LoginPage() {
  const { login } = useApp();
  const navigate = useNavigate();
  const [form, setForm] = useState({ username: '', password: '' });
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  async function submit(e) {
    e.preventDefault();
    setError('');
    setBusy(true);
    try {
      await login(form.username.trim(), form.password);
      navigate('/chat');
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid min-h-[calc(100vh-5rem)] place-items-center px-5 py-10">
      <div className="glass w-full max-w-md rounded-3xl p-8 animate-fade-up">
        <h1 className="font-display text-5xl leading-tight">Welcome back.</h1>
        <p className="mb-8 mt-3 text-sm text-muted">Sign in to ask your documents.</p>

        <form onSubmit={submit} className="space-y-4">
          <Label>
            Username
            <Input className="mt-1.5" autoComplete="username" value={form.username} onChange={set('username')} />
          </Label>
          <Label>
            Password
            <Input
              className="mt-1.5"
              type="password"
              autoComplete="current-password"
              value={form.password}
              onChange={set('password')}
            />
          </Label>

          <Button variant="primary" type="submit" disabled={busy} className="w-full py-3">
            {busy ? 'Please wait…' : <>Sign in </>}
          </Button>

          {error && <p className="text-sm text-danger">{error}</p>}
        </form>

        <p className="mt-8 text-sm text-muted">
          New organisation?{' '}
          <Link to="/register" className="text-ink underline-offset-4 transition hover:underline">Create an account</Link>
        </p>
      </div>
    </div>
  );
}