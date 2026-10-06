import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
    MessageSquare, Upload, Users, FolderTree, Library, ShieldCheck,
    MessageCircleWarning, ScrollText, Plus, LogOut, Search,
    CornerDownLeft, ArrowUp, ArrowDown, Sun, Moon,
} from 'lucide-react';
import { useApp } from '../context/AppContext';
import { useTheme } from '../context/ThemeContext';
import { Kbd, cx } from './ui';

export default function CommandPalette() {
    const [open, setOpen] = useState(false);
    const [q, setQ] = useState('');
    const [active, setActive] = useState(0);
    const inputRef = useRef(null);

    const { threads, logout, me } = useApp();
    const { theme, toggle } = useTheme();
    const navigate = useNavigate();

    // ⌘K / Ctrl+K to open, Escape to close
    useEffect(() => {
        const onKey = (e) => {
            const mod = e.metaKey || e.ctrlKey;
            if (mod && e.key.toLowerCase() === 'k') {
                e.preventDefault();
                setOpen((v) => !v);
            } else if (e.key === 'Escape' && open) {
                setOpen(false);
            }
        };
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [open]);

    useEffect(() => {
        if (open) {
            setQ('');
            setActive(0);
            setTimeout(() => inputRef.current?.focus(), 30);
        }
    }, [open]);

    const commands = useMemo(() => {
        const base = [
            { id: 'new', icon: Plus, label: 'New chat', group: 'Actions', run: () => navigate('/chat') },
            { id: 'chat', icon: MessageSquare, label: 'Open chat', group: 'Navigate', run: () => navigate('/chat') },
            { id: 'upload', icon: Upload, label: 'Upload document', group: 'Navigate', run: () => navigate('/upload') },
            {
                id: 'theme',
                icon: theme === 'dark' ? Sun : Moon,
                label: theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode',
                group: 'Actions',
                run: toggle,
            },
            { id: 'signout', icon: LogOut, label: 'Sign out', group: 'Actions', tone: 'danger', run: logout },
        ];

        if (me?.role === 'admin') {
            base.push(
                { id: 'users', icon: Users, label: 'Admin · Users', group: 'Admin', run: () => navigate('/admin/users') },
                { id: 'groups', icon: FolderTree, label: 'Admin · Groups', group: 'Admin', run: () => navigate('/admin/groups') },
                { id: 'collections', icon: Library, label: 'Admin · Collections', group: 'Admin', run: () => navigate('/admin/collections') },
                { id: 'guardrails', icon: ShieldCheck, label: 'Admin · Guardrails', group: 'Admin', run: () => navigate('/admin/guardrails') },
                { id: 'feedback', icon: MessageCircleWarning, label: 'Admin · Feedback', group: 'Admin', run: () => navigate('/admin/feedback') },
                { id: 'audit', icon: ScrollText, label: 'Admin · Audit log', group: 'Admin', run: () => navigate('/admin/audit') },
            );
        }

        threads.slice(0, 8).forEach((t) => {
            base.push({
                id: `t-${t.thread_id}`,
                icon: MessageSquare,
                label: t.title || 'Untitled chat',
                group: 'Recent chats',
                run: () => navigate(`/chat/${t.thread_id}`),
            });
        });

        return base;
    }, [threads, me, navigate, logout, theme, toggle]);

    const filtered = useMemo(() => {
        if (!q.trim()) return commands;
        const s = q.toLowerCase();
        return commands.filter(
            (c) => c.label.toLowerCase().includes(s) || c.group.toLowerCase().includes(s)
        );
    }, [q, commands]);

    const groups = useMemo(() => {
        const map = new Map();
        filtered.forEach((c) => {
            if (!map.has(c.group)) map.set(c.group, []);
            map.get(c.group).push(c);
        });
        return [...map.entries()];
    }, [filtered]);

    const flat = useMemo(() => groups.flatMap(([, items]) => items), [groups]);

    useEffect(() => {
        if (!open) return;
        const onKey = (e) => {
            if (e.key === 'ArrowDown') { e.preventDefault(); setActive((i) => Math.min(i + 1, flat.length - 1)); }
            if (e.key === 'ArrowUp') { e.preventDefault(); setActive((i) => Math.max(i - 1, 0)); }
            if (e.key === 'Enter') {
                e.preventDefault();
                const cmd = flat[active];
                if (cmd) { cmd.run?.(); setOpen(false); }
            }
        };
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [open, flat, active]);

    if (!open) return null;

    let cursor = -1;

    return (
        <div className="fixed inset-0 z-[150] grid place-items-start justify-center p-4 pt-[12vh]">
            <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={() => setOpen(false)} />

            {/* Wider + shorter palette */}
            <div className="glass-strong relative w-full max-w-4xl overflow-hidden rounded-2xl animate-fade-up">

                {/* Input row */}
                <div className="flex items-center gap-3 border-b border-border px-5 py-3.5">
                    <Search size={16} className="text-muted" />
                    <input
                        ref={inputRef}
                        value={q}
                        onChange={(e) => { setQ(e.target.value); setActive(0); }}
                        placeholder="Search chats, pages, actions…"
                        className="flex-1 bg-transparent text-sm text-ink placeholder:text-faint focus:outline-none"
                    />
                    <Kbd>esc</Kbd>
                </div>

                {/* Two-column list when wide enough; single column on narrow screens */}
                <div className="max-h-[46vh] overflow-y-auto p-2">
                    {groups.length === 0 && (
                        <div className="px-3 py-10 text-center text-sm text-muted">No results for “{q}”.</div>
                    )}


                    {groups.map(([group, items]) => (
                        <div key={group} className="mb-1">
                            <div className="px-3 pb-1 pt-2 text-[11px] font-medium uppercase tracking-wider text-faint">
                                {group}
                            </div>
                            {items.map((cmd) => {
                                cursor += 1;
                                const isActive = cursor === active;
                                const Icon = cmd.icon;
                                return (
                                    <button
                                        key={cmd.id}
                                        onMouseEnter={() => setActive(flat.indexOf(cmd))}
                                        onClick={() => { cmd.run?.(); setOpen(false); }}
                                        className={cx(
                                            'flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left text-sm transition',
                                            isActive ? 'bg-surface-2' : 'hover:bg-surface-2/60',
                                            cmd.tone === 'danger' && 'text-danger'
                                        )}
                                    >
                                        <Icon
                                            size={16}
                                            strokeWidth={1.8}
                                            className={cx('shrink-0', cmd.tone === 'danger' ? 'text-danger' : 'text-muted')}
                                        />
                                        <span className="flex-1 truncate">{cmd.label}</span>
                                        {isActive && <CornerDownLeft size={14} className="text-faint" />}
                                    </button>
                                );
                            })}
                        </div>
                    ))}

                </div>

                {/* Footer */}
                <div className="flex items-center justify-between border-t border-border px-5 py-2.5 text-[11px] text-faint">
                    <div className="flex items-center gap-3">
                        <span className="flex items-center gap-1"><ArrowUp size={12} /><ArrowDown size={12} /> navigate</span>
                        <span className="flex items-center gap-1"><CornerDownLeft size={12} /> open</span>
                        <span className="flex items-center gap-1"><Kbd>esc</Kbd> close</span>
                    </div>
                    <span>Docs Assistant</span>
                </div>
            </div>
        </div>
    );
}