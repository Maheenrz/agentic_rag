import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../lib/api';
import { Button, Input, Label } from '../components/ui';

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState('');
  const [sent, setSent] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setError('');

    const value = email.trim().toLowerCase();
    if (!EMAIL_RE.test(value)) return setError('Enter a valid email address.');

    setBusy(true);
    try {
      await api('/auth/forgot-password', { method: 'POST', body: { email: value } });
      setSent(true);   // always show the same message — don't leak whether the email exists
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid min-h-[calc(100vh-5rem)] place-items-center px-5 py-10">
      <div className="glass w-full max-w-md rounded-3xl p-8 animate-fade-up">
        <h1 className="font-display text-5xl leading-tight">Reset your password.</h1>

        {sent ? (
          <p className="mt-6 text-sm text-muted">
            If an account exists for <strong className="text-ink">{email}</strong>, we've sent a link to reset your password.
            Check your inbox (and spam folder).
          </p>
        ) : (
          <>
            <p className="mb-8 mt-3 text-sm text-muted">
              Enter your work email and we'll send you a reset link.
            </p>
            <form onSubmit={submit} className="space-y-4">
              <Label>
                Email
                <Input
                  className="mt-1.5"
                  type="email"
                  autoComplete="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </Label>
              <Button variant="primary" type="submit" disabled={busy} className="w-full py-3">
                {busy ? 'Sending…' : 'Send reset link'}
              </Button>
              {error && <p className="text-sm text-danger">{error}</p>}
            </form>
          </>
        )}

        <p className="mt-8 text-sm text-muted">
          <Link to="/login" className="text-ink underline-offset-4 hover:underline">Back to sign in</Link>
        </p>
      </div>
    </div>
  );
}