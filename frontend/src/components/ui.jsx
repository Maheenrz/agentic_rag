import { useEffect, useRef, useState } from 'react';

export function cx(...parts) {
  return parts.filter(Boolean).join(' ');
}

/* ---------------- Button ---------------- */
const VARIANTS = {
  primary: 'bg-accent text-canvas font-semibold border-transparent hover:bg-accent-hover',
  subtle:  'bg-surface-2 text-ink border-border hover:bg-surface-3',
  ghost:   'bg-transparent text-muted border-transparent hover:text-ink hover:bg-surface-2/60',
  danger:  'bg-transparent text-danger border-border hover:border-danger',
  glass:   'glass text-ink hover:border-[color:var(--color-blue)]',
};
const SIZES = {
  md:   'rounded-full px-4 py-2 text-sm',
  sm:   'rounded-full px-3 py-1 text-xs',
  icon: 'rounded-full h-9 w-9 grid place-items-center p-0',
};

export function Button({ variant = 'subtle', size = 'md', className, type = 'button', ...props }) {
  return (
    <button
      type={type}
      className={cx(
        'border transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-50',
        VARIANTS[variant], SIZES[size], className
      )}
      {...props}
    />
  );
}

/* ---------------- Fields ---------------- */
const field =
  'rounded-xl border border-border bg-surface-2 px-3.5 py-2.5 text-sm text-ink placeholder:text-faint focus:border-accent focus:ring-2 focus:ring-accent/25 focus:outline-none transition';

export const Input    = ({ className, ...p }) => <input    className={cx(field, 'w-full', className)} {...p} />;
export const Textarea = ({ className, ...p }) => <textarea className={cx(field, 'w-full', className)} {...p} />;
export const Select   = ({ className, children, ...p }) => (
  <select className={cx(field, className)} {...p}>{children}</select>
);
export const Label = ({ className, children }) => (
  <label className={cx('mb-1.5 block text-xs font-medium text-muted', className)}>{children}</label>
);

/* ---------------- Cards ---------------- */
export function Card({ className, children }) {
  return <div className={cx('rounded-2xl border border-border bg-surface p-6', className)}>{children}</div>;
}
export function GlassCard({ className, children }) {
  return <div className={cx('glass rounded-2xl p-6', className)}>{children}</div>;
}

/* ---------------- Badge / Avatar ---------------- */
export function Badge({ children, tone = 'default', className }) {
  const tones = {
    default: 'border-border text-muted',
    accent:  'border-accent/40 text-accent bg-accent/10',
    danger:  'border-danger/40 text-danger bg-danger/10',
    success: 'border-success/40 text-success bg-success/10',
  };
  return (
    <span className={cx('inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs', tones[tone], className)}>
      {children}
    </span>
  );
}
export const Pill = Badge;

export function Avatar({ name, size = 32, className }) {
  const initial = (name || '?').charAt(0).toUpperCase();
  return (
    <div
      className={cx(
        'grid shrink-0 place-items-center rounded-full bg-surface-3 text-xs font-semibold uppercase ring-1 ring-white/10',
        className
      )}
      style={{ width: size, height: size }}
    >
      {initial}
    </div>
  );
}

/* ---------------- Popover ---------------- */
export function Popover({ trigger, children, align = 'right', side = 'bottom', className, width }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onKey);
    return () => { document.removeEventListener('mousedown', onDoc); document.removeEventListener('keydown', onKey); };
  }, [open]);
  return (
    <div className="relative" ref={ref}>
      <div onClick={() => setOpen((v) => !v)}>{trigger}</div>
      {open && (
        <div
          style={{ width }}
          className={cx(
            'glass-strong absolute z-50 min-w-[220px] rounded-2xl p-2 text-sm animate-fade-up',
            side === 'top' ? 'bottom-full mb-2' : 'top-full mt-2',
            align === 'right' ? 'right-0' : 'left-0',
            className
          )}
        >
          {typeof children === 'function' ? children({ close: () => setOpen(false) }) : children}
        </div>
      )}
    </div>
  );
}

/* ---------------- MenuItem (for popovers) ---------------- */
export function MenuItem({ icon: Icon, label, hint, onClick, tone = 'default' }) {
  return (
    <button
      onClick={onClick}
      className={cx(
        'flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left text-sm transition',
        tone === 'danger' ? 'text-danger hover:bg-danger/10' : 'text-ink hover:bg-surface-2'
      )}
    >
      {Icon && <Icon size={16} strokeWidth={1.85} className="shrink-0" />}
      <span className="flex-1 truncate">{label}</span>
      {hint && <span className="text-xs text-faint">{hint}</span>}
    </button>
  );
}

/* ---------------- Modal ---------------- */
export function Modal({ open, onClose, title, description, children, footer, size = 'md' }) {
  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose?.();
    if (open) document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);
  if (!open) return null;
  const widths = { sm: 'max-w-sm', md: 'max-w-lg', lg: 'max-w-2xl' };
  return (
    <div className="fixed inset-0 z-[100] grid place-items-center p-4">
      <div className="absolute inset-0 bg-black/65 backdrop-blur-sm" onClick={onClose} />
      <div className={cx('glass-strong relative w-full rounded-2xl p-6 animate-fade-up', widths[size])}>
        {title && <h3 className="font-display text-2xl leading-tight">{title}</h3>}
        {description && <p className="mt-1 text-sm text-muted">{description}</p>}
        {children && <div className="mt-5">{children}</div>}
        {footer && <div className="mt-6 flex justify-end gap-2">{footer}</div>}
      </div>
    </div>
  );
}

/* ---------------- Drawer ---------------- */
export function Drawer({ open, onClose, side = 'right', title, children }) {
  return (
    <div className={cx('fixed inset-0 z-[90]', open ? 'pointer-events-auto' : 'pointer-events-none')}>
      <div
        className={cx('absolute inset-0 bg-black/55 backdrop-blur-sm transition-opacity', open ? 'opacity-100' : 'opacity-0')}
        onClick={onClose}
      />
      <aside
        className={cx(
          'glass-strong absolute top-0 h-full w-full max-w-md p-6 transition-transform duration-300',
          side === 'right' ? 'right-0' : 'left-0',
          open ? 'translate-x-0' : side === 'right' ? 'translate-x-full' : '-translate-x-full'
        )}
      >
        {title && <h3 className="mb-4 font-display text-2xl">{title}</h3>}
        {children}
      </aside>
    </div>
  );
}

/* ---------------- EmptyState / Skeleton / Kbd / Loading ---------------- */
export function EmptyState({ icon: Icon, title, body, action }) {
  return (
    <div className="m-auto max-w-sm text-center animate-fade-up">
      {Icon && (
        <div className="mx-auto mb-5 grid h-12 w-12 place-items-center rounded-2xl glass text-accent">
          <Icon size={22} strokeWidth={1.75} />
        </div>
      )}
      <h3 className="font-display text-3xl leading-tight">{title}</h3>
      {body && <p className="mt-2.5 text-sm text-muted">{body}</p>}
      {action && <div className="mt-6">{action}</div>}
    </div>
  );
}
export function Skeleton({ className }) {
  return <div className={cx('animate-pulse rounded-lg bg-surface-2', className)} />;
}
export function Kbd({ children }) {
  return (
    <kbd className="rounded border border-border bg-surface-2 px-1.5 py-0.5 font-sans text-[11px] text-muted">
      {children}
    </kbd>
  );
}
export const Loading = ({ error }) => (
  <p className={error ? 'text-danger' : 'text-muted'}>{error || 'Loading…'}</p>
);

/* ---------------- Table / Check / Stat ---------------- */
export function Table({ head, rows }) {
  return (
    <div className="overflow-x-auto rounded-2xl border border-border bg-surface/60">
      <table className="w-full border-collapse text-sm">
        <thead className="bg-surface-2/60">
          <tr>{head.map((h, i) => (
            <th key={i} className="border-b border-border px-4 py-3 text-left text-xs font-medium text-muted">{h}</th>
          ))}</tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-b border-border/60 last:border-0 hover:bg-surface-2/40">
              {row.map((cell, j) => <td key={j} className="px-4 py-3 align-top">{cell}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
export function Check({ label, ...props }) {
  return (
    <label className="flex items-center gap-2.5 py-1 text-sm">
      <input type="checkbox" className="h-4 w-4 accent-accent" {...props} />
      {label}
    </label>
  );
}
export function Stat({ value, label }) {
  return (
    <div className="glass rounded-2xl p-5">
      <div className="font-display text-3xl text-ink">{value}</div>
      <div className="mt-1 text-xs text-muted">{label}</div>
    </div>
  );
}