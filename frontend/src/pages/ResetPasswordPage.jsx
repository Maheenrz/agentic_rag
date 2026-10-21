import { useEffect, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { api } from '../lib/api';
import { Button, Input, Label } from '../components/ui';

const MIN_PASSWORD_CHARS = 8;
const MAX_PASSWORD_BYTES = 72;

export default function ResetPasswordPage() {
  const [params] = useSearchParams();
  const token = params.get('token') || '';
  const navigate = useNavigate();

  const [meta, setMeta] = useState(null);   // { username, email }
  const [error, setError] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPw, setConfirmPw] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!token) return setError('This reset link is missing its token.');
    api(`/auth/reset/${encodeURIComponent(token)}`)
      .then(setMeta)
      .catch((e) => setError(e.message));
  }, [token]);

  async function submit(e) {
    e.preventDefault();
    setError('');

    if (password.length < MIN_PASSWORD_CHARS)
      return setError(`Password must be at least ${MIN_PASSWORD_CHARS} characters.`);
    if (new TextEncoder().encode(password).length > MAX_PASSWORD_BYTES)
      return setError('Password is too long.');
    if (password !== confirmPw)
      return setError('Passwords do not match.');

    setBusy(true);
    try {
      await api('/auth/reset-password', { method: 'POST', body: { token, password } });
      navigate('/login', { replace: true });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  if (error && !meta) {
    return (
      <div className="grid min-h-[calc(100vh-5rem)] place-items-center px-5 py-10">
        <div className="glass w-full max-w-md rounded-3xl p-8 text-center">
          <h1 className="font-display text-4xl">Link problem</h1>
          <p className="mt-3 text-sm text-danger">{error}</p>
          <p className="mt-6 text-sm text-muted">
            <Link className="underline" to="/forgot-password">Request a new link</Link>
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="grid min-h-[calc(100vh-5rem)] place-items-center px-5 py-10">
      <div className="glass w-full max-w-md rounded-3xl p-8 animate-fade-up">
        <h1 className="font-display text-5xl leading-tight">Choose a new password.</h1>
        {meta && <p className="mb-8 mt-3 text-sm text-muted">For <strong className="text-ink">{meta.email}</strong></p>}

        <form onSubmit={submit} className="space-y-4">
          <Label>
            New password
            <Input
              className="mt-1.5"
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </Label>
          <Label>
            Confirm password
            <Input
              className="mt-1.5"
              type="password"
              autoComplete="new-password"
              value={confirmPw}
              onChange={(e) => setConfirmPw(e.target.value)}
            />
          </Label>

          <Button variant="primary" type="submit" disabled={busy || !meta} className="w-full py-3">
            {busy ? 'Saving…' : 'Set new password'}
          </Button>

          {error && <p className="text-sm text-danger">{error}</p>}
        </form>
      </div>
    </div>
  );
}