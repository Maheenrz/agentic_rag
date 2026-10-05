import { NavLink, useLocation, useNavigate } from 'react-router-dom';
import {
  Library, Lock, LogOut, MessageSquare, Pencil, Plus, ShieldCheck, Trash2, Upload,
} from 'lucide-react';
import { useApp } from '../context/AppContext';
import { api } from '../lib/api';
import { guard } from '../lib/hooks';
import { cx } from './ui';

const navClass = ({ isActive }) =>
  cx(
    'flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition',
    isActive ? 'bg-surface-2 text-ink' : 'text-muted hover:bg-surface-2/60 hover:text-ink'
  );

function SectionTitle({ children, count }) {
  return (
    <div className="mb-2 flex items-center justify-between px-3 text-xs font-medium text-muted">
      <span>{children}</span>
      {count != null && <span>{count}</span>}
    </div>
  );
}

export default function Sidebar() {
  const { me, threads, collections, selected, toggleCollection, refreshThreads, logout } = useApp();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const isAdmin = me.role === 'admin';

  async function rename(t) {
    const title = prompt('Rename chat', t.title);
    if (!title?.trim()) return;
    await api(`/threads/${t.thread_id}`, { method: 'PATCH', body: { title: title.trim() } });
    refreshThreads();
  }

  async function remove(t) {
    if (!confirm('Delete this chat and its messages?')) return;
    await api(`/threads/${t.thread_id}`, { method: 'DELETE' });
    if (pathname === `/chat/${t.thread_id}`) navigate('/chat');
    refreshThreads();
  }

  const navItems = [
    { to: '/chat', label: 'Chat', Icon: MessageSquare },
    { to: '/upload', label: 'Upload', Icon: Upload },
    ...(isAdmin ? [{ to: '/admin', label: 'Admin', Icon: ShieldCheck }] : []),
  ];

  return (
    <aside className="flex min-h-0 flex-col border-r border-border bg-surface">
      <div className="px-4 pt-6">
        <NavLink to="/" className="block px-3 font-display text-2xl leading-none">
          Docs Assistant
        </NavLink>

        <NavLink
          to="/chat"
          className="mt-6 flex w-full items-center justify-center gap-2 rounded-full bg-accent py-2.5 text-sm font-semibold text-canvas transition hover:bg-accent-hover"
        >
          <Plus size={16} strokeWidth={2.25} />
          New chat
        </NavLink>

        <nav className="mt-5 flex flex-col gap-0.5">
          {navItems.map(({ to, label, Icon }) => (
            <NavLink key={to} to={to} className={navClass}>
              <Icon size={18} strokeWidth={1.75} className="shrink-0" />
              {label}
            </NavLink>
          ))}
        </nav>
      </div>

      <div className="min-h-0 flex-1 space-y-7 overflow-y-auto px-4 pb-4 pt-7">
        <section>
          <SectionTitle count={threads.length}>Chats</SectionTitle>
          {threads.length === 0 && <p className="px-3 text-sm text-muted">No chats yet.</p>}
          {threads.map((t) => (
            <div key={t.thread_id} className="group flex items-center gap-1">
              <NavLink
                to={`/chat/${t.thread_id}`}
                className={({ isActive }) =>
                  cx(
                    'flex min-w-0 flex-1 items-center gap-3 rounded-lg px-3 py-2 text-sm transition',
                    isActive ? 'bg-surface-2 text-ink' : 'text-muted hover:bg-surface-2/60 hover:text-ink'
                  )
                }
              >
                <MessageSquare size={16} strokeWidth={1.75} className="shrink-0" />
                <span className="truncate">{t.title}</span>
              </NavLink>

              <div className="hidden items-center gap-0.5 pr-1 group-hover:flex">
                <button
                  onClick={guard(() => rename(t))}
                  title="Rename"
                  className="rounded p-1 text-muted hover:text-ink"
                >
                  <Pencil size={14} strokeWidth={1.75} />
                </button>
                <button
                  onClick={guard(() => remove(t))}
                  title="Delete"
                  className="rounded p-1 text-muted hover:text-danger"
                >
                  <Trash2 size={14} strokeWidth={1.75} />
                </button>
              </div>
            </div>
          ))}
        </section>

        <section>
          <SectionTitle>Search in</SectionTitle>
          <p className="mb-3 px-3 text-xs leading-relaxed text-muted">
            Nothing ticked searches everything you can access.
          </p>
          {collections.length === 0 && <p className="px-3 text-sm text-muted">No collections yet.</p>}
          {collections.map((c) => {
            const isPrivate = Boolean(c.owner_user_id);
            const Icon = isPrivate ? Lock : Library;
            return (
              <label
                key={c.collection_id}
                className="flex cursor-pointer items-center gap-3 rounded-lg px-3 py-2 text-sm text-muted transition hover:bg-surface-2/60 hover:text-ink"
              >
                <input
                  type="checkbox"
                  className="h-4 w-4 shrink-0 accent-accent"
                  checked={selected.includes(c.collection_id)}
                  onChange={() => toggleCollection(c.collection_id)}
                />
                <Icon size={16} strokeWidth={1.75} className="shrink-0" />
                <span className="truncate">
                  {isPrivate ? 'My private documents' : c.name}
                </span>
              </label>
            );
          })}
        </section>
      </div>

      <div className="border-t border-border p-3">
        <div className="flex items-center gap-3 rounded-xl px-2 py-2 transition hover:bg-surface-2/60">
          <div className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-canvas text-xs font-semibold uppercase ring-1 ring-border">
            {me.username.charAt(0)}
          </div>
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-medium">{me.username}</div>
            <div className="truncate text-xs text-muted">
              {me.org.name} · {me.role}
            </div>
          </div>
          <button
            onClick={logout}
            title="Sign out"
            className="rounded p-1.5 text-muted transition hover:text-ink"
          >
            <LogOut size={17} strokeWidth={1.75} />
          </button>
        </div>
      </div>
    </aside>
  );
}