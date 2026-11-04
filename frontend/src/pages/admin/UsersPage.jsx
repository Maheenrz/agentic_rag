
import { useState } from 'react';
import { useApp } from '../../context/AppContext';
import { api } from '../../lib/api';
import { guard, useAsync } from '../../lib/hooks';
import {
  Button,
  Card,
  Input,
  Label,
  Loading,
  Pill,
  Select,
  Table,
} from '../../components/ui';

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;
const USERNAME_RE = /^[A-Za-z0-9_.@-]{3,64}$/;

const EMPTY = { username: '', email: '', role: 'member' };

export default function UsersPage() {
  const { me } = useApp();
  const users = useAsync(() => api('/org/users'));

  const [form, setForm] = useState(EMPTY);
  const [formError, setFormError] = useState('');
  const [successMessage, setSuccessMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [resendingUserId, setResendingUserId] = useState(null);

  if (!users.data) {
    return <Loading error={users.error} />;
  }

  const set = (key) => (e) => {
    setForm((current) => ({
      ...current,
      [key]: e.target.value,
    }));
  };

  async function create(e) {
    e.preventDefault();
    setFormError('');
    setSuccessMessage('');

    const username = form.username.trim();
    const email = form.email.trim().toLowerCase();

    if (!USERNAME_RE.test(username)) {
      setFormError(
        'Username must be 3–64 characters: letters, digits, _ . @ -'
      );
      return;
    }

    if (!EMAIL_RE.test(email)) {
      setFormError('Enter a valid email address.');
      return;
    }

    if (
      users.data.some(
        (u) => u.email?.toLowerCase() === email
      )
    ) {
      setFormError('A user with that email already exists.');
      return;
    }

    setBusy(true);

    try {
      await api('/org/users', {
        method: 'POST',
        body: {
          username,
          email,
          role: form.role,
        },
      });

      setForm(EMPTY);
      setSuccessMessage(
        'Invitation created. The user should receive an invitation email.'
      );

      await users.reload();
    } catch (err) {
      setFormError(
        err?.message || 'Could not create the invitation.'
      );
    } finally {
      setBusy(false);
    }
  }

  async function remove(u) {
    const confirmed = window.confirm(
      `Delete ${u.username}? Their chats and private documents will be removed.`
    );

    if (!confirmed) return;

    setFormError('');
    setSuccessMessage('');

    try {
      await api(`/org/users/${u.user_id}`, {
        method: 'DELETE',
      });

      setSuccessMessage('User deleted.');
      await users.reload();
    } catch (err) {
      setFormError(err?.message || 'Could not delete this user.');
    }
  }

  async function resend(u) {
    if (resendingUserId !== null) return;

    setFormError('');
    setSuccessMessage('');
    setResendingUserId(u.user_id);

    try {
      await api(`/org/users/${u.user_id}/invite`, {
        method: 'POST',
      });

      setSuccessMessage(
        `A new invitation email has been requested for ${u.username}.`
      );

      await users.reload();
    } catch (err) {
      setFormError(
        err?.message || 'Could not resend the invitation.'
      );
    } finally {
      setResendingUserId(null);
    }
  }

  return (
    <div className="space-y-6">
      <Card>
        <h2 className="mb-3 text-lg font-semibold">
          Invite a user
        </h2>

        <p className="mb-3 text-sm text-muted">
          They'll get an email with a link to set their own password.
        </p>

        <form onSubmit={guard(create)} className="space-y-2">
          <div className="flex flex-wrap items-end gap-3">
            <Label className="min-w-[160px] flex-1">
              Username
              <Input
                className="mt-1"
                value={form.username}
                onChange={set('username')}
                required
              />
            </Label>

            <Label className="min-w-[200px] flex-1">
              Email
              <Input
                className="mt-1"
                type="email"
                value={form.email}
                onChange={set('email')}
                required
              />
            </Label>

            <Label>
              Role
              <Select
                className="mt-1"
                value={form.role}
                onChange={set('role')}
              >
                <option value="member">Member</option>
                <option value="admin">Admin</option>
              </Select>
            </Label>

            <Button
              variant="primary"
              type="submit"
              disabled={busy}
            >
              {busy ? 'Inviting…' : 'Send invite'}
            </Button>
          </div>

          {formError && (
            <p role="alert" className="text-sm text-danger">
              {formError}
            </p>
          )}

          {successMessage && (
            <p role="status" className="text-sm text-muted">
              {successMessage}
            </p>
          )}
        </form>
      </Card>

      <Card>
        <h2 className="mb-3 text-lg font-semibold">
          Members
        </h2>

        <Table
          head={['Username', 'Email', 'Role', 'Status', '']}
          rows={users.data.map((u) => [
            u.username,
            u.email || '—',
            <Pill key={`${u.user_id}-role`}>
              {u.role}
            </Pill>,
            <Pill key={`${u.user_id}-status`}>
              {u.password_set ? 'active' : 'invited'}
            </Pill>,
            u.user_id === me.user_id ? (
              <span className="text-xs text-muted">
                (you)
              </span>
            ) : (
              <div className="flex gap-2">
                {!u.password_set && (
                  <Button
                    size="sm"
                    disabled={resendingUserId !== null}
                    onClick={guard(() => resend(u))}
                  >
                    {resendingUserId === u.user_id
                      ? 'Resending…'
                      : 'Resend invite'}
                  </Button>
                )}

                <Button
                  size="sm"
                  variant="danger"
                  onClick={guard(() => remove(u))}
                >
                  Delete
                </Button>
              </div>
            ),
          ])}
        />
      </Card>
    </div>
  );
}
