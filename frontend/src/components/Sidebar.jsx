import { NavLink, useLocation, useNavigate } from 'react-router-dom';
import {
  Library, Lock, LogOut, MessageSquare, Pencil, Plus, Settings, ShieldCheck,
  Trash2, Upload, User, Users, FolderTree, ScrollText, MessageCircleWarning,
  Sun, Moon,
} from 'lucide-react';
import { useApp } from '../context/AppContext';
import { useTheme } from '../context/ThemeContext';
import { api } from '../lib/api';
import { guard } from '../lib/hooks';
import { Avatar, Button, MenuItem, Popover, cx } from './ui';

const navClass = ({ isActive }) =>
  cx(
    'flex items-center gap-3 rounded-xl px-3 py-2 text-sm transition',
    isActive ? 'bg-surface-2 text-ink' : 'text-muted hover:bg-surface-2/60 hover:text-ink'
  );

function SectionTitle({ children, count }) {
  return (
    <div className="mb-2 flex items-center justify-between px-3 text-[11px] font-medium uppercase tracking-wider text-faint">
      <span>{children}</span>
      {count != null && <span>{count}</span>}
    </div>
  );
}

export default function Sidebar() {
  const { me, threads, collections, selected, toggleCollection, refreshThreads, logout } = useApp();
  const { theme, toggle } = useTheme();      // ← the hook
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
    { to: '/chat',   label: 'Chat',   Icon: MessageSquare },
    { to: '/upload', label: 'Upload', Icon: Upload },
  ];

  return (
    <aside className="glass flex min-h-0 flex-col rounded-2xl border border-border">
      <div className="p-3">
        <Button
          variant="primary"
          onClick={() => navigate('/chat')}
          className="flex w-full items-center justify-center gap-2 py-2.5"
        >
          <Plus size={16} strokeWidth={2.4} /> New chat
        </Button>
      </div>

      <nav className="flex flex-col gap-0.5 px-3">
        {navItems.map(({ to, label, Icon }) => (
          <NavLink key={to} to={to} className={navClass}>
            <Icon size={18} strokeWidth={1.75} className="shrink-0" />
            {label}
          </NavLink>
        ))}
      </nav>

      <div className="min-h-0 flex-1 space-y-6 overflow-y-auto px-3 pb-3 pt-5">
        <section>
          <SectionTitle count={threads.length}>Chats</SectionTitle>
          {threads.length === 0 && <p className="px-3 text-sm text-muted">No chats yet.</p>}
          {threads.map((t) => (
            <div key={t.thread_id} className="group flex items-center gap-1">
              <NavLink
                to={`/chat/${t.thread_id}`}
                className={({ isActive }) =>
                  cx(
                    'flex min-w-0 flex-1 items-center gap-3 rounded-xl px-3 py-2 text-sm transition',
                    isActive ? 'bg-surface-2 text-ink' : 'text-muted hover:bg-surface-2/60 hover:text-ink'
                  )
                }
              >
                <MessageSquare size={16} strokeWidth={1.75} className="shrink-0" />
                <span className="truncate">{t.title}</span>
              </NavLink>
              <div className="hidden items-center gap-0.5 pr-1 group-hover:flex">
                <button onClick={guard(() => rename(t))} title="Rename" className="rounded p-1 text-muted hover:text-ink">
                  <Pencil size={14} strokeWidth={1.75} />
                </button>
                <button onClick={guard(() => remove(t))} title="Delete" className="rounded p-1 text-muted hover:text-danger">
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
                className="flex cursor-pointer items-center gap-3 rounded-xl px-3 py-2 text-sm text-muted transition hover:bg-surface-2/60 hover:text-ink"
              >
                <input
                  type="checkbox"
                  className="h-4 w-4 shrink-0 accent-accent"
                  checked={selected.includes(c.collection_id)}
                  onChange={() => toggleCollection(c.collection_id)}
                />
                <Icon size={16} strokeWidth={1.75} className="shrink-0" />
                <span className="truncate">{isPrivate ? 'My private documents' : c.name}</span>
              </label>
            );
          })}
        </section>
      </div>

      <div className="border-t border-border p-3">
        <div className="flex items-center gap-3 rounded-xl px-1.5 py-1.5">
          <Avatar name={me.username} />
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-medium">{me.username}</div>
            <div className="truncate text-xs text-muted">
              {me.org?.name || 'No Organization'} · {me.role}
            </div>
          </div>

          <Popover
            align="right"
            side="top"
            trigger={
              <button title="Settings" className="rounded-full p-2 text-muted transition hover:bg-surface-2 hover:text-ink">
                <Settings size={17} strokeWidth={1.9} />
              </button>
            }
          >
            {({ close }) => (
              <div className="flex flex-col">
                <div className="px-3 py-2 text-[11px] uppercase tracking-wider text-faint">Account</div>
                <MenuItem icon={User} label="Profile" onClick={() => { close(); navigate('/profile'); }} />

                {/* ─── Theme toggle lives HERE (inside the gear popover) ─── */}
                <MenuItem
                  icon={theme === 'dark' ? Sun : Moon}
                  label={theme === 'dark' ? 'Light mode' : 'Dark mode'}
                  onClick={() => { toggle(); close(); }}
                />

                {isAdmin && (
                  <>
                    <div className="mt-1 px-3 py-2 text-[11px] uppercase tracking-wider text-faint">Admin</div>
                    <MenuItem icon={Users}                label="Users"       onClick={() => { close(); navigate('/admin/users'); }} />
                    <MenuItem icon={FolderTree}           label="Groups"      onClick={() => { close(); navigate('/admin/groups'); }} />
                    <MenuItem icon={Library}              label="Collections" onClick={() => { close(); navigate('/admin/collections'); }} />
                    <MenuItem icon={ShieldCheck}          label="Guardrails"  onClick={() => { close(); navigate('/admin/guardrails'); }} />
                    <MenuItem icon={MessageCircleWarning} label="Feedback"    onClick={() => { close(); navigate('/admin/feedback'); }} />
                    <MenuItem icon={ScrollText}           label="Audit log"   onClick={() => { close(); navigate('/admin/audit'); }} />
                  </>
                )}

                <div className="my-1 h-px bg-border" />
                <MenuItem icon={LogOut} label="Sign out" tone="danger" onClick={logout} />
              </div>
            )}
          </Popover>
        </div>
      </div>
    </aside>
  );
}