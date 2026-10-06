import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ArrowRight } from 'lucide-react';
import { useApp } from '../context/AppContext';
import { Button, Input, Label } from '../components/ui';

const USERNAME_RE = /^[A-Za-z0-9_.@-]{3,64}$/;
const MIN_PASSWORD_CHARS = 8;
const MAX_PASSWORD_BYTES = 72;

function validateRegister(username, password) {
  if (!USERNAME_RE.test(username)) return 'Username must be 3–64 characters: letters, digits, _ . @ -';
  if (password.length < MIN_PASSWORD_CHARS) return `Password must be at least ${MIN_PASSWORD_CHARS} characters.`;
  if (new TextEncoder().encode(password).length > MAX_PASSWORD_BYTES)
  return 'Password is too long. Try a shorter one (or fewer accented / non-Latin characters).';
  return '';
}

export default function Register() {
  const { register } = useApp();
  const navigate = useNavigate();
  const [form, setForm] = useState({ username: '', password: '', org: '' });
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  async function submit(e) {
    e.preventDefault();
    setError('');
    const username = form.username.trim();
    const problem = validateRegister(username, form.password);
    if (problem) return setError(problem);

    setBusy(true);
    try {
      await register(username, form.password, form.org.trim());
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
        <h1 className="font-display text-5xl leading-tight">Create your organisation.</h1>
        <p className="mb-8 mt-3 text-sm text-muted">
          You will be the admin. You can add people and collections after this.
        </p>

        <form onSubmit={submit} className="space-y-4">
          <Label>
            Username
            <Input className="mt-1.5" autoComplete="username" value={form.username} onChange={set('username')} />
            <span className="mt-1.5 block text-xs font-normal text-muted">3–64 characters: letters, digits, _ . @ -</span>
          </Label>
          <Label>
            Password
            <Input className="mt-1.5" type="password" autoComplete="new-password" value={form.password} onChange={set('password')} />
            <span className="mt-1.5 block text-xs font-normal text-muted">At least 8 characters.</span>
          </Label>
          <Label>
            Organisation name
            <Input className="mt-1.5" placeholder="e.g. Acme Ltd" value={form.org} onChange={set('org')} />
          </Label>

          <Button variant="primary" type="submit" disabled={busy} className="w-full py-3">
            {busy ? 'Please wait…' : <>Create account </>}
          </Button>

          {error && <p className="text-sm text-danger">{error}</p>}
        </form>

        <p className="mt-8 text-sm text-muted">
          Already have an account?{' '}
          <Link to="/login" className="text-ink underline-offset-4 transition hover:underline">Sign in</Link>
        </p>
      </div>
    </div>
  );
}