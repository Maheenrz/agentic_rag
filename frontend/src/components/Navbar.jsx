import { Link, NavLink, useNavigate } from 'react-router-dom';
import {
  Sparkles, LogIn, UserPlus, LayoutDashboard, Search,
  LogOut, User as UserIcon, Settings, Sun, Moon,
} from 'lucide-react';
import { useApp } from '../context/AppContext';
import { useTheme } from '../context/ThemeContext';
import { Avatar, Button, Popover, MenuItem, Kbd, cx } from './ui';
import ThemeToggle from './ThemeToggle';

const linkClass = ({ isActive }) =>
  cx(
    'rounded-full px-4 py-2 text-sm transition',
    isActive ? 'text-ink bg-surface-2' : 'text-muted hover:text-ink hover:bg-surface-2/60'
  );

export default function Navbar() {
  const { me, logout } = useApp();
  const { theme, toggle } = useTheme();     // ← the hook
  const navigate = useNavigate();

  return (
    <header className="sticky top-0 z-40 px-3 pt-3">
      <div className="glass mx-auto flex h-14 max-w-[1400px] items-center gap-3 rounded-2xl px-3">

        {/* Brand */}
        <Link to={me ? '/chat' : '/'} className="flex items-center gap-2 pl-1 pr-2">
          <span className="grid h-8 w-8 place-items-center rounded-xl bg-accent text-canvas">
            <Sparkles size={16} strokeWidth={2.2} />
          </span>
          <span className="font-display text-xl leading-none">Docs Assistant</span>
        </Link>

        {/* Center nav */}
        <nav className="hidden flex-1 items-center justify-center gap-3 md:flex">
          {!me && (
            <>
              <NavLink to="/" className={linkClass} end>Home</NavLink>
              {/* <a href="#principles" className={linkClass}>Principles</a> */}
              <a href="#how" className={linkClass}>How it works</a>
            </>
          )}
          {me && (
            <>
              <NavLink to="/chat" className={linkClass}>Chat</NavLink>
              <NavLink to="/upload" className={linkClass}>Upload</NavLink>
            </>
          )}
        </nav>

        {/* Right side */}
        <div className="ml-auto flex items-center gap-2">

          {/* Cmd+K hint button */}
          <button
            onClick={() => {
              const e = new KeyboardEvent('keydown', { key: 'k', metaKey: true });
              document.dispatchEvent(e);
            }}
            className="hidden items-center gap-2 rounded-full border border-border bg-surface-2/60 px-3 py-1.5 text-xs text-muted transition hover:text-ink md:flex"
          >
            <Search size={13} />
            <span>Search</span>
            <Kbd>⌘K</Kbd>
          </button>

          {/* Standalone theme toggle (visible on md+) */}
          <ThemeToggle className="hidden md:grid" />

          {!me ? (
            <>
              <Button variant="ghost" onClick={() => navigate('/login')} className="hidden md:inline-flex">
                <LogIn size={15} className="mr-1.5" /> Sign in
              </Button>
              <Button variant="primary" onClick={() => navigate('/register')}>
             Get started
              </Button>
            </>
          ) : (
            <>
              <Button variant="primary" onClick={() => navigate('/chat')} className="hidden md:inline-flex">
                <LayoutDashboard size={15} className="mr-1.5" /> Open app
              </Button>

              <Popover
                align="right"
                trigger={
                  <button className="rounded-full ring-1 ring-border transition hover:ring-[color:var(--color-blue)]">
                    <Avatar name={me.username} size={34} />
                  </button>
                }
              >
                {({ close }) => (
                  <div className="flex flex-col">
                    <div className="px-3 py-2">
                      <div className="text-sm font-medium">{me.username}</div>
                      <div className="text-xs text-muted">
                        {me.org?.name || 'No Organization'} · {me.role}
                      </div>
                    </div>

                    <div className="my-1 h-px bg-border" />

                    <MenuItem icon={UserIcon} label="Profile"  onClick={() => { close(); navigate('/profile'); }} />
                    <MenuItem icon={Settings} label="Settings" onClick={() => { close(); navigate('/chat'); }} hint="⌘," />

                    {/* ─── Theme toggle lives HERE (inside the popover) ─── */}
                    <MenuItem
                      icon={theme === 'dark' ? Sun : Moon}
                      label={theme === 'dark' ? 'Light mode' : 'Dark mode'}
                      onClick={() => { toggle(); close(); }}
                    />

                    <div className="my-1 h-px bg-border" />
                    <MenuItem icon={LogOut} label="Sign out" tone="danger" onClick={logout} />
                  </div>
                )}
              </Popover>
            </>
          )}
        </div>
      </div>
    </header>
  );
}